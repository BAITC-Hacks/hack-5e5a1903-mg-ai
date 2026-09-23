"""Разбор .docx положения в плоский список пунктов с составными идентификаторами.

Что здесь важно и что проверяется прототипом:
- номер пункта берется из текста абзаца ("5.3.2."), буквенные подпункты ("а.") получают
  составной id вида 5.3.2.а, потому что буквы начинаются заново в каждом пункте;
- склеенные в один абзац пункты ("... 3.10.Рабочие места ... 3.11.По вопросам ...")
  режутся по ожидаемому следующему номеру, а не по любому числу с точкой, иначе
  ссылки вроде "п. 5.8.1 и 5.8.2" внутри текста порвут пункт;
- владелец функции стоит не в самом пункте, а во вводной фразе блока ("5.3. Директоры
  департаментов ...:" или отдельной строкой "Главный аудитор:"), поэтому он наследуется вниз
  через стек областей. Вложенная вводная фраза ("5.3.2. организуют ...:") область не закрывает
  и владельца не меняет, она только группирует подпункты;
- модальность (обязанность / право / запрет) берется из ближайшей вводной фразы, где она
  вообще упомянута, а не из глагола в пункте. Последнее упоминание побеждает: "обязаны ...,
  а также имеют право:" вводит список прав.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.\s*(.*)$", re.S)
LETTERED = re.compile(r"^([а-яё])\.\s+(.*)$", re.S)
DASHED = re.compile(r"^[–—-]\s+(.*)$", re.S)
TOC_LINE = re.compile(r"^\d+\.\s+[А-ЯЁ\s\.,]+\s\d+$")
VERB_FIRST = re.compile(r"^[А-Яа-яЁё]+(ет|ют|ит|ят|ует|ают|яют|ится|ются|ется)\b")
NOT_ROLE_START = ("Для ", "В ", "При ", "По ", "На ", "С ", "Проверки ", "Программа ")

MODALITY_PATTERNS = [
    ("prohibition", re.compile(r"не имеют? права")),
    ("right", re.compile(r"имеют? право|вправе")),
    ("duty", re.compile(r"обязан")),
]
OWNER_CUT = re.compile(r"обязан|имеют? право|не имеют? права|вправе|подчиня|осуществля")


@dataclass
class Clause:
    id: str
    num: str | None
    text: str
    section: str
    owner: str | None
    modality: str
    doc: str
    order: int
    is_lead: bool

    @property
    def is_leaf(self) -> bool:
        return not self.is_lead

    @property
    def depth(self) -> int:
        return self.num.count(".") + 1 if self.num else 0


@dataclass
class Scope:
    num: str | None  # None для безномерной вводной строки
    depth: int | None  # для безномерной: глубина первого пункта под ней
    owner: str | None
    modality: str | None
    lead_text: str = ""
    role_like: bool = False


def paragraphs(path: str) -> list[str]:
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.find(W + "body").iter(W + "p"):
        t = "".join(n.text or "" for n in p.iter(W + "t")).strip()
        t = re.sub(r"\s+", " ", t)
        if t and not TOC_LINE.match(t):
            out.append(t)
    return out


def next_sibling(num: str) -> str:
    parts = num.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def split_inline(num: str, text: str, section: str | None = None) -> list[tuple[str, str]]:
    """Режет "3.9. текст 3.10.текст 3.11.текст" на пункты по ожидаемым номерам.

    Следующий раздел ("13.") тоже может приклеиться к концу абзаца. Его режем строже:
    только если сразу за точкой идет заглавная буква, иначе порвем "протокол No 4."."""
    pieces = []
    cur_num, cur_text = num, text
    while True:
        found = None
        for cand in (next_sibling(cur_num), cur_num + ".1"):
            m = re.search(r"(?:(?<=\s)|(?<=\.))" + re.escape(cand) + r"\.(?!\d)\s*", cur_text)
            if m and m.start() > 0 and (found is None or m.start() < found[0]):
                found = (m.start(), m.end(), cand)
        if section and section.isdigit():
            cand = str(int(section) + 1)
            m = re.search(r"(?<=\s)" + re.escape(cand) + r"\.(?=[А-ЯЁ])\s*", cur_text)
            if m and m.start() > 0 and (found is None or m.start() < found[0]):
                found = (m.start(), m.end(), cand)
        if not found:
            pieces.append((cur_num, cur_text.strip()))
            return pieces
        start, end, cand = found
        pieces.append((cur_num, cur_text[:start].strip()))
        cur_num, cur_text = cand, cur_text[end:]


def modality_in(lead: str) -> str | None:
    best: tuple[str | None, int] = (None, -1)
    for name, pat in MODALITY_PATTERNS:
        for m in pat.finditer(lead):
            if m.start() > best[1]:
                best = (name, m.start())
    return best[0]


def is_role_like(lead: str) -> bool:
    text = lead.strip()
    if not text or text.startswith(NOT_ROLE_START):
        return False
    return not VERB_FIRST.match(text)


def owner_of(lead: str) -> str:
    text = lead.rstrip(":").strip()
    m = OWNER_CUT.search(text)
    owner = text[: m.start()] if m else text
    owner = re.sub(r"\s+(и|а также|,)$", "", owner.strip(" ,;"))
    return owner[:90]


def parse(path: str, doc: str) -> list[Clause]:
    """Разбор docx. Для других форматов см. proto/load.py, там абзацы приходят
    из своего извлекателя и передаются в parse_paragraphs."""
    return parse_paragraphs(paragraphs(path), doc)


def parse_paragraphs(paras: list[str], doc: str, lead_resolver=None) -> list[Clause]:
    """lead_resolver(text) -> (role_like, owner, modality | None). Если задан, владельца
    и модальность вводной фразы определяет он (модель по схеме), иначе эвристика ниже."""
    clauses: list[Clause] = []
    seen_ids: set[str] = set()
    stack: list[Scope] = []
    section = "0"
    last_num: str | None = None
    order = 0
    dash_counter: dict[str, int] = {}

    def resolve() -> tuple[str | None, str]:
        owner = next((s.owner for s in reversed(stack) if s.role_like and s.owner), None)
        modality = next((s.modality for s in reversed(stack) if s.modality), None)
        if modality is None:
            modality = "duty" if owner else "none"
        return owner, modality

    def emit(cid, num, text, is_lead):
        nonlocal order
        order += 1
        if cid in seen_ids:
            # два подперечня с буквами под одним пунктом (9.3.а дважды): второй получает суффикс,
            # иначе пункты сливаются в словарях по идентификатору
            n = 2
            while f"{cid}({n})" in seen_ids:
                n += 1
            cid = f"{cid}({n})"
        seen_ids.add(cid)
        owner, modality = resolve()
        clauses.append(Clause(cid, num, text, section, owner, "none" if is_lead else modality, doc, order, is_lead))

    def pop_for(num: str, is_lead: bool):
        depth = num.count(".") + 1
        while stack:
            top = stack[-1]
            if top.num is None:
                if top.depth is None:
                    top.depth = depth
                    break
                if depth < top.depth or (depth == top.depth and is_lead and is_role_like(top.lead_text) is False):
                    stack.pop()
                    continue
                if depth == top.depth and is_lead:
                    stack.pop()
                    continue
                break
            if num == top.num or not num.startswith(top.num + "."):
                stack.pop()
                continue
            break

    def push_lead(num: str | None, text: str):
        if lead_resolver is not None:
            role_like, owner, modality = lead_resolver(text)
        else:
            role_like = is_role_like(text)
            owner = owner_of(text) if role_like else None
            modality = modality_in(text)
        stack.append(Scope(num=num, depth=None, owner=owner if role_like else None, modality=modality, lead_text=text, role_like=role_like))

    def handle_numbered(num: str, text: str):
        nonlocal section, last_num
        if "." not in num:
            section = num
            stack.clear()
            emit(num, num, text, is_lead=True)
            last_num = num
            return
        is_lead = text.endswith(":")
        pop_for(num, is_lead)
        emit(num, num, text, is_lead=is_lead)
        last_num = num
        if is_lead:
            push_lead(num, text)

    for para in paras:
        m = NUMBERED.match(para)
        if m:
            for num, text in split_inline(m.group(1), m.group(2), section):
                handle_numbered(num, text)
            continue

        m = LETTERED.match(para)
        if m and last_num:
            letter, text = m.group(1), m.group(2)
            pieces = split_inline(last_num, "X " + text, section)
            own_text = pieces[0][1][1:].strip() if pieces[0][1].startswith("X") else pieces[0][1]
            emit(f"{last_num}.{letter}", last_num, own_text, is_lead=own_text.endswith(":"))
            for num, extra in pieces[1:]:
                handle_numbered(num, extra)
            continue

        m = DASHED.match(para)
        if m and last_num:
            dash_counter[last_num] = dash_counter.get(last_num, 0) + 1
            emit(f"{last_num}.-{dash_counter[last_num]}", last_num, m.group(1), is_lead=False)
            continue

        if para.endswith(":"):
            emit(f"{last_num or section}.lead{order + 1}", None, para, is_lead=True)
            push_lead(None, para)
        else:
            emit(f"{last_num or section}.txt{order + 1}", None, para, is_lead=False)

    return clauses


def departments(clauses: list[Clause]) -> dict[str, str]:
    """Аббревиатура -> полное имя, из пункта 3.4."""
    out = {}
    for c in clauses:
        if c.num == "3.4" and c.id != "3.4":
            m = re.match(r"(Департамент [^()]+?)\s*\((\S+)\)", c.text)
            if m:
                out[m.group(2).strip(".")] = m.group(1).strip()
    return out


def positions_under_chief(clauses: list[Clause]) -> list[str]:
    """Должности, подчиненные Главному аудитору, из пункта 3.5."""
    return [c.text.rstrip(".") for c in clauses if c.num == "3.5" and c.id != "3.5"]
