"""Веб-интерфейс: главная страница, готовые отчёты примеров, решения специалиста «Исключить / Вернуть»."""
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

import app.web.main as web

ROOT = Path(__file__).resolve().parent.parent


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(web, "RUNS_DIR", tmp_path)
    return TestClient(web.app)


def test_index_and_example_reports(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    assert c.get("/healthz").json()["ok"] is True
    page = c.get("/").text
    assert "Открыть отчёт" in page and "/examples/full/paper" in page and ">Рассчитать</button>" in page
    assert "<h1>Методика расчёта</h1>" in c.get("/method").text
    rep = c.get("/examples/no_keys/paper")
    assert rep.status_code == 200 and "Готовый отчёт по примеру" in rep.text
    assert "/examples/no_keys/paper/obosnovanie_nmcd.docx" in rep.text
    assert c.get("/examples/no_keys/paper/report.xlsx").status_code == 200
    assert c.get("/examples/full/../../etc").status_code == 404


def test_exclude_then_return_restores_calculation(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    run_id = "a1b2c3d4e5f6"
    shutil.copytree(ROOT / "examples" / "reports_no_keys" / "paper", tmp_path / run_id)
    data = json.loads((tmp_path / run_id / "report.json").read_text(encoding="utf-8"))
    data["id"] = run_id
    (tmp_path / run_id / "report.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    before = c.get(f"/api/runs/{run_id}").json()["items"][0]
    item = data["items"][0]
    offer = next(d["offer_id"] for d in item["decisions"] if d["included"])
    body = {"item_id": item["item"]["id"], "offer_id": offer, "include": False, "reason": "цена с доставкой в регион"}
    assert c.post(f"/runs/{run_id}/override", json=body).status_code == 200
    excluded = c.get(f"/api/runs/{run_id}").json()["items"][0]
    assert excluded["unit_price"] != before["unit_price"]
    assert c.post(f"/runs/{run_id}/override", json={**body, "include": True, "reason": "ошибка"}).status_code == 200
    back = c.get(f"/api/runs/{run_id}").json()["items"][0]
    assert back["unit_price"] == before["unit_price"] and back["message"] == before["message"]
    saved = json.loads((tmp_path / run_id / "report.json").read_text(encoding="utf-8"))
    assert len(saved["settings"]["override_log"]) == 2
    assert any("отменил своё решение" in t["message"] for t in saved["trace"])


def test_prices_from_commercial_offers(tmp_path, monkeypatch):
    # позиция без цены (цитокины, режим без ключей) + три КП → цена обоснована; в DOCX нет наименований поставщиков
    import io
    from datetime import date, timedelta
    import docx
    c = _client(tmp_path, monkeypatch)
    run_id = "b1c2d3e4f5a6"
    shutil.copytree(ROOT / "examples" / "reports_no_keys" / "cytokines", tmp_path / run_id)
    data = json.loads((tmp_path / run_id / "report.json").read_text(encoding="utf-8"))
    data["id"] = run_id
    (tmp_path / run_id / "report.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    item_id = data["items"][1]["item"]["id"]
    assert c.get(f"/api/runs/{run_id}").json()["items"][1]["status"] != "final"
    day = (date.today() - timedelta(days=3)).isoformat()
    kp = [("ООО «Альфа Реагент»", "7707083893", "26 000"), ("ООО «Бета Лаб»", "", "24 500,50"),
          ("ИП Иванов", "", "27 100")]
    for name, inn, price in kp:
        r = c.post(f"/runs/{run_id}/manual", json={"item_id": item_id, "supplier": name, "inn": inn, "price": price,
                                                   "price_date": day, "note": "исх. № 15"})
        assert r.status_code == 200, r.text
    it = c.get(f"/api/runs/{run_id}").json()["items"][1]
    assert it["status"] == "final" and it["unit_price"] == "25866.83"
    doc = docx.Document(io.BytesIO(c.get(f"/runs/{run_id}/report.docx").content))
    text = "\n".join(p.text for p in doc.paragraphs) + "\n".join(
        cell.text for t in doc.tables for row in t.rows for cell in row.cells)
    assert "Коммерческое предложение № 1" in text and "Альфа" not in text and "Иванов" not in text
    bad = {"item_id": item_id, "supplier": "ООО «Гамма»", "price": "100", "price_date": day}
    assert c.post(f"/runs/{run_id}/manual", json={**bad, "inn": "1234567890"}).status_code == 400
    assert c.post(f"/runs/{run_id}/manual", json={**bad, "price": "0"}).status_code == 400
    future = (date.today() + timedelta(days=5)).isoformat()
    assert c.post(f"/runs/{run_id}/manual", json={**bad, "price_date": future}).status_code == 400


def test_error_pages_for_people_and_api(tmp_path, monkeypatch):
    c = _client(tmp_path, monkeypatch)
    page = c.get("/runs/000000000000", headers={"Accept": "text/html"})
    assert page.status_code == 404 and "Перейти к новому расчёту" in page.text
    assert c.get("/api/runs/000000000000").json()["detail"] == "Расчёт не найден"
    assert c.get("/nope", headers={"Accept": "text/html"}).status_code == 404


def _wait_done(c, run_id, seconds=10.0):
    import time
    end = time.time() + seconds
    while time.time() < end:
        st = c.get(f"/api/runs/{run_id}").json()
        if st.get("status") in ("done", "failed"):
            return st
        time.sleep(0.2)
    return st


def test_broken_or_empty_file_ends_with_clear_error(tmp_path, monkeypatch):
    """Повреждённый DOCX и PDF без текста не оставляют страницу «Идёт расчёт» висеть: отчёт с понятной причиной."""
    c = _client(tmp_path, monkeypatch)
    for name, data, words in (("tz.docx", b"not a zip at all", "не удалось прочитать"),
                              ("tz.txt", b"   ", "нет текста")):
        r = c.post("/runs", files={"file": (name, data)}, data={"use_llm": "off", "use_web": "off"},
                   follow_redirects=False)
        run_id = r.headers["location"].rsplit("/", 1)[-1]
        assert _wait_done(c, run_id)["status"] == "failed"
        page = c.get(f"/runs/{run_id}").text
        assert "Расчёт не выполнен" in page and words in page and "Скачать обоснование" not in page


def test_public_stand_limits(tmp_path, monkeypatch):
    """Общедоступный стенд: на главной только свои расчёты; длинный текст и переполненная очередь — понятный отказ."""
    c = _client(tmp_path, monkeypatch)
    for rid in ("aaaaaaaaaaa1", "bbbbbbbbbbb2"):
        shutil.copytree(ROOT / "examples" / "reports_no_keys" / "printer", tmp_path / rid)
        data = json.loads((tmp_path / rid / "report.json").read_text(encoding="utf-8"))
        data["id"] = rid
        (tmp_path / rid / "report.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(web.settings, "public", True)
    c.cookies.set(web.COOKIE, "aaaaaaaaaaa1")
    page = c.get("/").text
    assert "/runs/aaaaaaaaaaa1" in page and "/runs/bbbbbbbbbbb2" not in page
    r = c.post("/runs", data={"text": "x" * 100_001}, headers={"Accept": "text/html"})
    assert r.status_code == 413 and "100 000" in r.text
    monkeypatch.setattr(web.settings, "max_queue", 0)
    r = c.post("/runs", data={"text": "Бумага А4 — 10 пачек"}, headers={"Accept": "text/html"})
    assert r.status_code == 503 and "Сервис занят" in r.text
