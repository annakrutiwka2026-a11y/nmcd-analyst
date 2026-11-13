"""Чтение входного документа: DOCX, PDF или текст. Сохраняет порядок абзацев и таблиц."""
from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader


@dataclass
class Document:
    name: str
    sha256: str
    text: str                                   # текст с таблицами в виде строк «ячейка | ячейка»
    tables: list[list[list[str]]] = field(default_factory=list)


def _clean(s: str) -> str:
    return " ".join((s or "").replace(" ", " ").split())


def _table_rows(table: Table) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in table.rows:
        cells: list[str] = []
        prev = None
        for cell in row.cells:
            # объединённые ячейки python-docx возвращает несколько раз — оставляем одну
            if prev is not None and cell._tc is prev:
                continue
            prev = cell._tc
            cells.append("\n".join(_clean(p.text) for p in cell.paragraphs if _clean(p.text)))
        rows.append(cells)
    return rows


def read_docx(data: bytes) -> tuple[str, list[list[list[str]]]]:
    d = docx.Document(io.BytesIO(data))
    lines: list[str] = []
    tables: list[list[list[str]]] = []
    for child in d.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            t = _clean(Paragraph(child, d).text)
            if t:
                lines.append(t)
        elif tag == "tbl":
            rows = _table_rows(Table(child, d))
            tables.append(rows)
            for r in rows:
                parts = [c.replace("\n", "; ") for c in r if c]
                if parts:
                    lines.append(" | ".join(parts))
    return "\n".join(lines), tables


def read_pdf(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def read_input(data: bytes, filename: str) -> Document:
    name = Path(filename or "описание.txt").name
    digest = hashlib.sha256(data).hexdigest()
    suffix = Path(name).suffix.lower()
    tables: list[list[list[str]]] = []
    if suffix == ".docx":
        text, tables = read_docx(data)
    elif suffix == ".pdf":
        text = read_pdf(data)
    else:
        text = data.decode("utf-8", "replace")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return Document(name=name, sha256=digest, text=text, tables=tables)
