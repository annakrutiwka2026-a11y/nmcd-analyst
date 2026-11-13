"""Веб-интерфейс: загрузка ТЗ, ход расчёта, отчёт, решения специалиста и выгрузки."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

from app.config import ROOT, get_settings
from app.engine.rules import load_rules
from app.ingest.reader import read_input
from app.models import Procurement, Report, TraceEvent
from app.pipeline import Run, add_manual_offer, recalculate, rnp_check
from app.report.fmt import MSK, dt as fmt_dt, money as fmt_money
from app.report.kp import render_kp_docx
from app.report.render import render_docx, render_html, render_json, render_xlsx, report_actions, save_all
from app.sources.web import inn_valid
from app.web.examples import EXAMPLE_REPORTS, EXAMPLES

settings = get_settings()
RUNS_DIR = settings.data_dir / "runs"
RUNS_DIR.mkdir(parents=True, exist_ok=True)
_env = Environment(loader=FileSystemLoader([str(Path(__file__).parent / "templates"),
                                             str(Path(__file__).parent.parent / "report" / "templates")]),
                   autoescape=select_autoescape(["html"]))
app = FastAPI(title="Обоснование НМЦД", docs_url="/api/docs", redoc_url=None)

_live: dict[str, dict[str, Any]] = {}
_slots = asyncio.Semaphore(2)
_ID = re.compile(r"^[a-f0-9]{12}$")
MAX_UPLOAD = 15 * 1024 * 1024
MAX_TEXT = 100_000
COOKIE = "nmcd_runs"
EXAMPLE_FILES = {"obosnovanie_nmcd.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "zapros_kp.docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "report.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 "report.json": "application/json"}


def _run_dir(run_id: str) -> Path:
    if not _ID.match(run_id):
        raise HTTPException(404, "Расчёт не найден")
    return RUNS_DIR / run_id


def _load(run_id: str) -> Report:
    path = _run_dir(run_id) / "report.json"
    if not path.is_file():
        raise HTTPException(404, "Расчёт не найден")
    return Report.model_validate_json(path.read_text(encoding="utf-8"))


def _my_runs(request: Request) -> list[str]:
    """Расчёты этого посетителя (cookie браузера): на общедоступном стенде чужие расчёты не показываются."""
    return [x for x in request.cookies.get(COOKIE, "").split(".") if _ID.match(x)][-12:]


def _recent(limit: int = 12, only: Optional[list[str]] = None) -> list[dict[str, Any]]:
    out = []
    paths = (RUNS_DIR.glob("*/report.json") if only is None
             else [RUNS_DIR / x / "report.json" for x in only if (RUNS_DIR / x / "report.json").is_file()])
    for p in sorted(paths, key=lambda x: x.stat().st_mtime, reverse=True)[:limit]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        try:
            created = fmt_dt(datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00")), with_time=True)
        except (KeyError, ValueError):
            created = "—"
        total = data.get("total")
        out.append({"id": data["id"], "title": data["procurement"]["title"], "created": created,
                    "total": fmt_money(Decimal(str(total))) if total not in (None, "") else None,
                    "status": data.get("status"), "all_final": data.get("all_final")})
    return out


def _failed(run_id: str, data: bytes, filename: str, message: str) -> Report:
    """Отчёт о расчёте, который не удалось начать: страница хода расчёта сразу покажет причину."""
    now = datetime.now(timezone.utc)
    name = Path(filename or "описание.txt").name
    proc = Procurement(title=name, items=[], source_name=name, source_sha256=hashlib.sha256(data).hexdigest())
    return Report(id=run_id, created_at=now, finished_at=now, procurement=proc, status="failed", error=message,
                  settings={"org_name": settings.org_name})


async def _execute(run_id: str, data: bytes, filename: str, use_llm: bool, use_web: bool) -> None:
    state = _live[run_id]

    def on_event(ev: TraceEvent) -> None:
        state["events"].append({"ts": ev.ts.astimezone(MSK).strftime("%H:%M:%S"), "stage": ev.stage,
                                "message": ev.message})

    try:
        async with _slots:
            try:
                doc = read_input(data, filename)
            except Exception as exc:  # повреждённый или не тот формат файла
                report = _failed(run_id, data, filename, f"Файл не удалось прочитать ({type(exc).__name__}). "
                                                          f"Сохраните ТЗ в DOCX или PDF с текстом и загрузите снова.")
            else:
                if len(doc.text.strip()) < 20:
                    report = _failed(run_id, data, filename, "В файле нет текста: возможно, это скан без текстового "
                                                             "слоя. Загрузите DOCX или PDF с текстом.")
                else:
                    run = Run(settings, run_id=run_id, progress=on_event, use_llm=use_llm, use_web=use_web)
                    report = await run.run(doc)
            report.settings["org_name"] = settings.org_name
            save_all(report, _run_dir(run_id), settings.org_name)
    except Exception as exc:  # отчёт об ошибке сохраняется всегда, чтобы страница хода расчёта не ждала вечно
        report = _failed(run_id, data, filename, f"Сбой расчёта: {type(exc).__name__}")
        (_run_dir(run_id) / "report.json").write_text(render_json(report), encoding="utf-8")
    finally:
        state["done"] = True


@app.exception_handler(StarletteHTTPException)
async def http_error(request, exc: StarletteHTTPException):
    """Для API — JSON, для страниц в браузере — понятная страница с возвратом на главную."""
    wants_html = "text/html" in request.headers.get("accept", "") and not request.url.path.startswith("/api/")
    if not wants_html:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    page = _env.get_template("error.html").render(code=exc.status_code, message=exc.detail)
    return HTMLResponse(page, status_code=exc.status_code)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "llm": settings.llm_enabled, "web": settings.web_enabled, "model": settings.llm_model}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> str:
    ready = {key: [mode for mode, folder in EXAMPLE_REPORTS.items()
                   if (ROOT / "examples" / folder / key / "report.json").is_file()] for key in EXAMPLES}
    recent = _recent(only=_my_runs(request) if settings.public else None)
    return _env.get_template("index.html").render(recent=recent, llm=settings.llm_enabled, web=settings.web_enabled,
                                                  model=settings.llm_model, examples=EXAMPLES, ready=ready)


def _example_dir(mode: str, key: str) -> Path:
    if mode not in EXAMPLE_REPORTS or key not in EXAMPLES:
        raise HTTPException(404, "Пример не найден")
    path = ROOT / "examples" / EXAMPLE_REPORTS[mode] / key
    if not (path / "report.json").is_file():
        raise HTTPException(404, "Отчёт по примеру не найден")
    return path


@app.get("/examples/{mode}/{key}", response_class=HTMLResponse)
async def example_report(mode: str, key: str) -> str:
    """Готовый отчёт по примеру (посчитан заранее), со ссылками на копии страниц-источников."""
    path = _example_dir(mode, key)
    report = Report.model_validate_json((path / "report.json").read_text(encoding="utf-8"))
    how = ("Цены взяты из реестра контрактов ЕИС и с сайтов поставщиков, техническое задание разобрано моделью."
           if mode == "full" else "Расчёт выполнен без ключей: цены только из реестра контрактов ЕИС, техническое задание разобрано по правилам.")
    banner = {"title": f"Готовый отчёт по примеру «{EXAMPLES[key][1]}»",
              "text": f"Расчёт выполнен заранее, чтобы не ждать. {how}"}
    base = f"/examples/{mode}/{key}/"
    actions = [a for a in report_actions(base, report) if (path / a["href"][len(base):]).is_file()]
    return render_html(report, links=True, actions=actions, banner=banner, app_nav=True)


@app.get("/examples/{mode}/{key}/{name}")
async def example_file(mode: str, key: str, name: str) -> Response:
    path = _example_dir(mode, key)
    if name not in EXAMPLE_FILES or not (path / name).is_file():
        raise HTTPException(404, "Файл не найден")
    return Response((path / name).read_bytes(), media_type=EXAMPLE_FILES[name],
                    headers={"Content-Disposition": f'attachment; filename="{key}_{mode}_{name}"'})


def _start(data: bytes, name: str, shown: str, use_llm: bool, use_web: bool) -> str:
    if sum(1 for st in _live.values() if not st["done"]) >= settings.max_queue:
        raise HTTPException(503, "Сервис занят: сейчас идут другие расчёты. Попробуйте через несколько минут")
    run_id = uuid.uuid4().hex[:12]
    _run_dir(run_id).mkdir(parents=True, exist_ok=True)
    _live[run_id] = {"events": [], "done": False, "title": shown, "started": datetime.now(timezone.utc).isoformat()}
    asyncio.create_task(_execute(run_id, data, name, use_llm, use_web))
    return run_id


@app.post("/runs")
async def start_run(request: Request, file: Optional[UploadFile] = File(None), text: str = Form(""),
                    example: str = Form(""), use_llm: str = Form("on"), use_web: str = Form("on")) -> RedirectResponse:
    if example:
        if example not in EXAMPLES:
            raise HTTPException(400, "Неизвестный пример")
        name = EXAMPLES[example][0]
        data = (ROOT / "examples" / "tz" / name).read_bytes()
    elif file is not None and file.filename:
        name = Path(file.filename).name
        if Path(name).suffix.lower() not in (".docx", ".pdf", ".txt", ".md"):
            raise HTTPException(400, "Поддерживаются файлы DOCX, PDF и TXT")
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "Файл больше 15 МБ")
    elif text.strip():
        if len(text) > MAX_TEXT:
            raise HTTPException(413, "Описание длиннее 100 000 знаков: загрузите ТЗ файлом")
        name, data = "описание.txt", text.strip().encode("utf-8")
    else:
        raise HTTPException(400, "Загрузите ТЗ или вставьте описание закупки")
    shown = EXAMPLES[example][1] if example else ("Описание закупки" if name == "описание.txt" else name)
    run_id = _start(data, name, shown, use_llm == "on", use_web == "on")
    resp = RedirectResponse(f"/runs/{run_id}", status_code=303)
    resp.set_cookie(COOKIE, ".".join(_my_runs(request) + [run_id]), max_age=90 * 24 * 3600, httponly=True,
                    samesite="lax")
    return resp


@app.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_page(run_id: str) -> str:
    report_path = _run_dir(run_id) / "report.json"
    state = _live.get(run_id)
    if state and not state["done"] or (not report_path.is_file() and state):
        return _env.get_template("progress.html").render(run_id=run_id, title=state["title"], started=state["started"])
    report = _load(run_id)
    actions = ([] if report.status == "failed" else
               report_actions(f"/runs/{run_id}/", report, docx="report.docx", kp="report.kp.docx"))
    return render_html(report, interactive=report.status != "failed", actions=actions, app_nav=True)


@app.get("/method", response_class=HTMLResponse)
async def method_page() -> str:
    return _env.get_template("method.html").render(rules=load_rules())


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    _run_dir(run_id)

    async def stream():
        sent = 0
        while True:
            state = _live.get(run_id)
            if state is None:
                yield "event: done\ndata: {}\n\n"
                return
            events = state["events"]
            while sent < len(events):
                yield f"data: {json.dumps(events[sent], ensure_ascii=False)}\n\n"
                sent += 1
            if state["done"]:
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


class ManualPrice(BaseModel):
    item_id: str
    supplier: str
    inn: str = ""
    price: str
    price_date: str           # ГГГГ-ММ-ДД
    no_vat: bool = False      # поставщик не плательщик НДС
    note: str = ""            # реквизиты КП


def _current_overrides(report: Report) -> dict[str, dict[str, tuple[bool, str]]]:
    current = report.settings.get("overrides") or {}
    return {item: {oid: (bool(v[0]), str(v[1])) for oid, v in d.items()} for item, d in current.items()}


class Override(BaseModel):
    item_id: str
    offer_id: str
    include: bool
    reason: str


@app.post("/runs/{run_id}/override")
async def override(run_id: str, body: Override) -> JSONResponse:
    report = _load(run_id)
    if not body.reason.strip():
        raise HTTPException(400, "Укажите причину")
    if not any(r.item.id == body.item_id and any(o.id == body.offer_id for o in r.offers) for r in report.items):
        raise HTTPException(404, "Предложение не найдено")
    ov = _current_overrides(report)
    item_ov = ov.setdefault(body.item_id, {})
    prev = item_ov.get(body.offer_id)
    reason = body.reason.strip()[:300]
    if prev is not None and prev[0] != body.include:
        # обратное действие снимает прежнее решение специалиста: предложение снова проходит общие правила
        item_ov.pop(body.offer_id)
        action = f"Специалист отменил своё решение по {body.offer_id}: {reason}"
    else:
        item_ov[body.offer_id] = (body.include, reason)
        action = f"Специалист {'вернул в расчёт' if body.include else 'исключил'} {body.offer_id}: {reason}"
    report.settings.setdefault("override_log", []).append(
        {"ts": datetime.now(timezone.utc).isoformat(), "item": body.item_id, "offer": body.offer_id,
         "include": body.include, "reason": reason})
    report.trace.append(TraceEvent(ts=datetime.now(timezone.utc), stage="override", message=action,
                                   data={"item": body.item_id, "offer": body.offer_id}))
    report = recalculate(report, ov)
    save_all(report, _run_dir(run_id), settings.org_name)
    return JSONResponse({"ok": True, "total": str(report.total) if report.total else None})


@app.post("/runs/{run_id}/manual")
async def add_manual(run_id: str, body: ManualPrice) -> JSONResponse:
    """Цена из коммерческого предложения: добавляется к позиции, расчёт пересчитывается по тем же правилам."""
    report = _load(run_id)
    res = next((r for r in report.items if r.item.id == body.item_id), None)
    if res is None:
        raise HTTPException(404, "Позиция не найдена")
    supplier = " ".join(body.supplier.split())[:200]
    if not supplier:
        raise HTTPException(400, "Укажите поставщика")
    inn = re.sub(r"\D", "", body.inn)
    if inn and not inn_valid(inn):
        raise HTTPException(400, "ИНН не прошёл проверку контрольной суммы")
    try:
        price = Decimal(re.sub(r"[\s\u00a0]", "", body.price).replace(",", ".")).quantize(Decimal("0.01"))
        day = date.fromisoformat(body.price_date)
    except (ArithmeticError, ValueError):
        raise HTTPException(400, "Цена — число, дата — в формате ГГГГ-ММ-ДД") from None
    if price <= 0:
        raise HTTPException(400, "Цена должна быть больше нуля")
    if day > date.today():
        raise HTTPException(400, "Дата коммерческого предложения не может быть в будущем")
    note = " ".join(body.note.split())[:300]
    rnp = rnp_url = None
    if inn:
        rnp, rnp_url = await rnp_check(settings, inn)
    offer = add_manual_offer(report, body.item_id, supplier, inn, price, day, body.no_vat, note)
    if inn:
        offer.extra.update({"rnp": rnp, "rnp_url": rnp_url, "legal_checked": True})
    report.settings.setdefault("manual_log", []).append(
        {"ts": datetime.now(timezone.utc).isoformat(), "item": body.item_id, "offer": offer.id, "supplier": supplier,
         "inn": inn, "price": str(price), "date": day.isoformat(), "no_vat": body.no_vat, "note": note})
    report.trace.append(TraceEvent(
        ts=datetime.now(timezone.utc), stage="override",
        message=f"Специалист добавил цену из коммерческого предложения {offer.id}: {fmt_money(price)} за "
                f"{res.item.unit}, {supplier}{', ИНН ' + inn if inn else ''}, от {day.strftime('%d.%m.%Y')}",
        data={"item": body.item_id, "offer": offer.id}))
    report = recalculate(report, _current_overrides(report))
    save_all(report, _run_dir(run_id), settings.org_name)
    return JSONResponse({"ok": True, "offer_id": offer.id, "total": str(report.total) if report.total else None})


@app.get("/runs/{run_id}/report.{fmt}")
async def download(run_id: str, fmt: str) -> Response:
    report = _load(run_id)
    if fmt == "json":
        return Response(render_json(report), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="nmcd_{run_id}.json"'})
    if fmt == "html":
        return Response(render_html(report), media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="nmcd_{run_id}.html"'})
    if fmt == "xlsx":
        return Response(render_xlsx(report),
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="nmcd_{run_id}.xlsx"'})
    if fmt == "docx":
        return Response(render_docx(report, settings.org_name),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="obosnovanie_nmcd_{run_id}.docx"'})
    if fmt == "kp.docx":
        return Response(render_kp_docx(report, settings.org_name),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": f'attachment; filename="zapros_kp_{run_id}.docx"'})
    raise HTTPException(404, "Формат не поддерживается")


@app.post("/api/runs")
async def api_start(file: Optional[UploadFile] = File(None), text: str = Form(""), use_llm: str = Form("on"),
                    use_web: str = Form("on")) -> JSONResponse:
    """Запуск расчёта из внешней системы. Возвращает идентификатор и адреса статуса и отчёта."""
    if file is not None and file.filename:
        name = Path(file.filename).name
        if Path(name).suffix.lower() not in (".docx", ".pdf", ".txt", ".md"):
            raise HTTPException(400, "Поддерживаются файлы DOCX, PDF и TXT")
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "Файл больше 15 МБ")
    elif text.strip() and len(text) <= MAX_TEXT:
        name, data = "описание.txt", text.strip().encode("utf-8")
    else:
        raise HTTPException(400, "Передайте файл ТЗ или описание закупки (до 100 000 знаков)")
    run_id = _start(data, name, name, use_llm == "on", use_web == "on")
    return JSONResponse({"id": run_id, "status": f"/api/runs/{run_id}", "report_json": f"/runs/{run_id}/report.json",
                         "page": f"/runs/{run_id}"}, status_code=202)


@app.get("/api/runs/{run_id}")
async def api_status(run_id: str) -> JSONResponse:
    path = _run_dir(run_id) / "report.json"
    state = _live.get(run_id)
    if not path.is_file():
        if state:
            return JSONResponse({"id": run_id, "status": "running", "events": state["events"][-5:]})
        raise HTTPException(404, "Расчёт не найден")
    report = _load(run_id)
    items = [{"number": r.item.number, "name": r.item.name, "status": r.calc.status.value if r.calc else None,
              "unit_price": str(r.calc.unit_price) if r.calc and r.calc.unit_price is not None else None,
              "total": str(r.calc.total) if r.calc and r.calc.total is not None else None,
              "message": r.calc.message if r.calc else None} for r in report.items]
    return JSONResponse({"id": run_id, "status": report.status, "total": str(report.total) if report.total else None,
                         "all_final": report.all_final, "items": items})


@app.get("/evidence/{name}")
async def evidence(name: str) -> Response:
    """Сохранённая копия страницы-источника. Скрипты страницы не выполняются (CSP sandbox)."""
    if not re.match(r"^[a-f0-9]{64}\.html\.gz$", name):
        raise HTTPException(404, "Копия не найдена")
    path = settings.data_dir / "evidence" / name
    if not path.is_file():
        path = ROOT / "examples" / "evidence" / name      # копии к готовым отчётам по примерам
    if not path.is_file():
        raise HTTPException(404, "Копия не найдена")
    body = gzip.decompress(path.read_bytes())
    return Response(body, media_type="text/html; charset=utf-8",
                    headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})
