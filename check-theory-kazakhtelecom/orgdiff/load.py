"""Загрузка документов трех форматов в единый список пунктов.

- docx: абзацы из XML, см. parse.paragraphs.
- pdf: текст извлекается построчно, и абзацы приходится восстанавливать. Правило:
  новый абзац начинается со структурного маркера (номер пункта, буква подпункта, тире),
  либо после строки, которая заметно короче полной ширины колонки, потому что последняя
  строка абзаца в выключенном по ширине тексте почти всегда короткая, а строка переноса
  почти всегда полная. Номер страницы в начале каждой страницы отбрасывается.
- xlsx: это не пункты, а таблица оргструктуры. Пунктов не дает, структуру дает
  proto.structure.from_xlsx.

Дальше все форматы идут через один parse_paragraphs, поэтому конвейер за загрузчиком
от формата не зависит.
"""

from __future__ import annotations

import re
from pathlib import Path

from .parse import DASHED, LETTERED, NUMBERED, Clause, paragraphs, parse_paragraphs

PAGE_NUMBER = re.compile(r"^\s*\d{1,3}\s*$")
# строка оглавления: номер раздела, короткий заголовок без знаков препинания, номер страницы
TOC_ENTRY = re.compile(r"^\d{1,2}(?:\.\d+)*\.?\s+[^;:]{3,70}\s\d{1,3}$")


def drop_toc_blocks(lines: list[str], min_block: int = 4) -> list[str]:
    """Оглавление в PDF приходит в обычном регистре и по форме неотличимо от заголовка
    раздела с номером страницы в конце. Отличает его только то, что таких строк
    подряд много. Блок из min_block и более подряд идущих строк-оглавления выбрасывается."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j < len(lines) and TOC_ENTRY.match(lines[j]):
            j += 1
        if j - i >= min_block:
            i = j
            continue
        out.append(lines[i])
        i += 1
    return out


def pdf_lines(path: str) -> list[str]:
    from pypdf import PdfReader

    out: list[str] = []
    for page in PdfReader(path).pages:
        lines = [ln.rstrip() for ln in (page.extract_text() or "").split("\n")]
        lines = [ln for ln in lines if ln.strip()]
        if lines and PAGE_NUMBER.match(lines[0]):
            lines = lines[1:]
        out.extend(lines)
    return out


def reflow(lines: list[str]) -> list[str]:
    """Строки PDF -> абзацы."""
    if not lines:
        return []
    lengths = sorted(len(ln) for ln in lines)
    full_width = lengths[int(len(lengths) * 0.9)] if lengths else 100
    short = 0.8 * full_width

    paras: list[str] = []
    cur = ""
    prev_ended = True
    for ln in lines:
        starts = bool(NUMBERED.match(ln) or LETTERED.match(ln) or DASHED.match(ln))
        if starts or prev_ended or not cur:
            if cur:
                paras.append(cur)
            cur = ln
        else:
            cur = cur + " " + ln
        prev_ended = len(ln) < short or ln.endswith(":")
    if cur:
        paras.append(cur)
    # артефакт экспорта Word: пробел с одной стороны дефиса, "ИТ -инциденты", "нормативно -правовые".
    # Симметричное " - " не трогаем: оно бывает и в исходнике как тире.
    return [re.sub(r"(?<=\S) -(?=\S)|(?<=\S)- (?=\S)", "-", p) for p in paras]


def load_clauses(path: str | Path, doc: str, resolver=None) -> list[Clause]:
    """resolver: необязательный LeadResolver из owners.py. Если он есть, вводные фразы
    сначала отдаются ему пачкой, и владельцев определяет модель, а не эвристика."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".docx":
        paras = paragraphs(str(p))
    elif ext == ".pdf":
        paras = reflow(drop_toc_blocks(pdf_lines(str(p))))
    elif ext in {".xlsx", ".xlsm"}:
        return []
    else:
        raise ValueError(f"неподдерживаемый формат: {p.name}")
    if resolver is not None:
        resolver.prime([x for x in paras if x.rstrip().endswith(":")])
    return parse_paragraphs(paras, doc, lead_resolver=resolver)


def load_set(paths: list[str | Path], doc: str, resolver=None):
    """Комплект документов одной стороны: пункты из docx и pdf, структура из xlsx,
    если он есть, иначе из пунктов."""
    from .structure import Structure, from_clauses, from_xlsx

    clauses: list[Clause] = []
    structure: Structure | None = None
    for path in paths:
        p = Path(path)
        if p.suffix.lower() in {".xlsx", ".xlsm"}:
            structure = from_xlsx(p)
        else:
            part = load_clauses(p, doc if len(paths) == 1 else f"{doc}:{p.name}", resolver=resolver)
            offset = len(clauses)
            for c in part:
                c.order += offset
            clauses.extend(part)
    if structure is None:
        structure = from_clauses(clauses)
    return clauses, structure
