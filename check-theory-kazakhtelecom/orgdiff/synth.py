"""Генератор синтетических положений с заранее известными изменениями.

Зачем: контрольный комплект один, и на нём прототип настраивался. Чтобы понять, работает
ли конвейер на других документах, нужны другие документы. Здесь из пула реальных
формулировок функций собираются положения с разным числом департаментов, разными
наборами функций и прав, а затем в «после» вносятся изменения восьми типов, каждое
с записью в таблицу ожиданий. Прогон по многим таким парам показывает полноту
и точность на документах, которых конвейер не видел.

Типы изменений:
- lose: функция удалена целиком;
- partial: у функции отрезан хвост-действие или срок;
- move: функция снята с одного департамента и добавлена другому;
- dup: функция скопирована ещё одному департаменту, у первого осталась;
- create: добавлен новый департамент со своими функциями и должностями;
- remove: департамент упразднён целиком;
- paraphrase: функция переформулирована без смены смысла, потерей быть не должна;
- insert: в начало блока вставлен новый пункт, чтобы сдвинуть нумерацию всех остальных.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from pathlib import Path

DEPT_NAMES = [
    ("Департамент операционного аудита", "ДОА"),
    ("Департамент ИТ-аудита", "ДИТА"),
    ("Департамент методологии и контроля качества", "ДМКК"),
    ("Департамент финансового аудита", "ДФА"),
    ("Департамент непрерывного мониторинга", "ДНМ"),
    ("Департамент анализа данных", "ДАД"),
    ("Департамент комплаенс-аудита", "ДКА"),
    ("Департамент аудита закупок", "ДАЗ"),
    ("Департамент аудита дочерних обществ", "ДАДО"),
]
POSITIONS = ["Директор проектов", "Руководитель направления", "Аудитор", "Старший аудитор", "Менеджер по аудиту", "Ведущий аналитик"]

FALLBACK_DUTIES = [
    "организует работу департамента в соответствии с планом работ БВА, осуществляя руководство и распределение обязанностей между работниками;",
    "готовит предложения для включения в план работ БВА;",
    "запрашивает у Руководителей Общества информацию, необходимую для осуществления функций БВА, контролирует своевременность и полноту ее предоставления;",
    "организует контроль устранения недостатков и нарушений, выявленных в ходе проведения проверок БВА;",
    "анализирует результаты проверок и готовит материалы и предложения в зоне ответственности для представления Главному аудитору;",
    "выносит предложения по повышению профессионального уровня работников департамента Главному аудитору;",
    "взаимодействует с Руководителями Общества по всему кругу вопросов, касающихся выполнения функций БВА;",
    "готовит материалы аудиторских проверок в зоне ответственности для представления Совету директоров и Комитету по аудиту;",
    "участвует в разработке внутренних нормативных документов БВА;",
    "организует проведение проверок по Обществу с целью обеспечения выполнения плана работ БВА;",
    "осуществляет тактическое планирование достижения целей внутреннего аудита с использованием знаний лучших практик;",
    "контролирует выполнение целей проверок и сроки выполнения графика проверки проектной командой;",
    "организует кросс-функциональное взаимодействие между проектной командой и владельцем объекта аудита;",
    "организует процесс согласования результатов проверок с Руководителями Общества по проверяемым направлениям;",
    "готовит отчеты об итогах выполнения плана работы БВА на ежеквартальной основе и по итогам года;",
    "разрабатывает методические материалы и актуализирует внутренние документы, регламентирующие деятельность внутреннего аудита;",
    "организует обучение работников БВА и консультирует по сложным вопросам аудита и подготовки отчетности;",
    "консолидирует полученные предложения по плану, формирует план работ БВА и представляет на рассмотрение Главному аудитору;",
    "организует непрерывный мониторинг качества деятельности внутреннего аудита;",
    "проводит анализ информации о реализовавшихся рисках, в том числе выявленных по результатам проверок;",
    "оценивает эффективность контрольных процедур и иных мероприятий по управлению рисками;",
    "проверяет полноту выявления и корректность оценки рисков на всех уровнях управления;",
    "координирует взаимодействие БВА с внешним аудитором Общества;",
    "осуществляет выполнение прочих поручений Главного аудитора.",
]
FALLBACK_RIGHTS = [
    "выносить предложения по внесению изменений в утвержденные программы проверок Главному аудитору;",
    "формировать группы контроля качества с привлечением работников БВА в соответствии с ресурсным планом;",
    "выносить предложения по объему и содержанию внешней оценки БВА Главному аудитору;",
    "использовать конфиденциальную информацию в соответствии с установленными правилами хранения, получения и передачи документов;",
    "вести переписку с Руководителями Общества по вопросам, входящим в зону ответственности;",
    "присутствовать на заседаниях органов управления подразделений Общества в качестве наблюдателя без права голоса;",
    "выносить предложения по поощрению и наложению взысканий на работников департамента;",
    "доводить до сведения Руководителей Общества результаты по запросу оказания консультационных услуг;",
    "требовать проведения полной или частичной инвентаризации имущества для установления его фактического наличия;",
    "получать отчеты о результатах аудиторской проверки внешних аудиторов и акты налоговых проверок Общества;",
]

SYNONYMS = [
    ("организует", "обеспечивает организацию"),
    ("готовит", "подготавливает"),
    ("запрашивает", "истребует"),
    ("контролирует", "осуществляет контроль за"),
    ("анализирует", "проводит анализ"),
    ("взаимодействует", "осуществляет взаимодействие"),
    ("участвует", "принимает участие"),
    ("выносит", "вносит"),
    ("Руководителей Общества", "руководителей структурных подразделений Общества"),
    ("в зоне ответственности", "в рамках зоны ответственности"),
]
TAILS = [
    " на ежеквартальной основе и по итогам года",
    " в соответствии с ресурсным планом",
    " с привлечением работников БВА",
    " и контролирует сроки выполнения графика проверки",
    " и организует контроль качества работы проектной команды",
    " Главному аудитору",
]


@dataclass
class Dept:
    name: str
    abbr: str
    positions: list[str]
    duties: list[str]
    rights: list[str]


@dataclass
class Doc:
    edition: int
    depts: list[Dept]
    general: list[str]


@dataclass
class Expectation:
    kind: str  # lose | partial | move | dup | create | remove | paraphrase | insert
    text: str = ""
    dept: str = ""
    dept_to: str = ""
    note: str = ""


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".;:,")


def load_pool(tz_dir: Path | None) -> tuple[list[str], list[str]]:
    """Пул формулировок: из реальных документов, если они есть, иначе встроенный."""
    duties, rights = list(FALLBACK_DUTIES), list(FALLBACK_RIGHTS)
    if tz_dir and tz_dir.exists():
        try:
            from .parse import parse

            for f in tz_dir.glob("*.docx"):
                if "редакция" not in f.name:
                    continue
                for c in parse(str(f), "pool"):
                    if c.section == "5" and c.is_leaf and c.num and c.owner and 40 < len(c.text) < 220 and c.id.count(".") == 2:
                        (rights if c.modality == "right" else duties).append(c.text)
        except Exception:  # noqa: BLE001
            pass

    # уникальные, без лишних пробелов. Две редакции дают почти одинаковые варианты одной
    # функции, из них оставляем один: ключ это первые шесть слов
    def uniq(xs):
        import difflib

        out: list[str] = []
        for x in xs:
            if not any(ch.isalpha() for ch in x):
                continue
            x = re.sub(r"\s+", " ", x).strip()
            nx = _norm(x)
            # близкие формулировки одной функции из двух редакций оставляем в одном экземпляре,
            # иначе «потеря» одной из них честно окажется «слиянием» с другой
            if any(difflib.SequenceMatcher(None, nx, _norm(y)).ratio() > 0.7 for y in out):
                continue
            out.append(x)
        return out

    return uniq(duties), uniq(rights)


def make_doc(rng: random.Random, duties_pool: list[str], rights_pool: list[str], n_depts: int) -> Doc:
    names = rng.sample(DEPT_NAMES, n_depts)
    duties_pool = duties_pool[:]
    rights_pool = rights_pool[:]
    rng.shuffle(duties_pool)
    rng.shuffle(rights_pool)
    depts = []
    di = ri = 0
    # функции между департаментами не повторяются: иначе «потеря» у одного будет
    # честным «слиянием» с таким же пунктом у другого, и ожидание генератора окажется ложным
    max_depts = max(1, min(n_depts, len(duties_pool) // 5, len(rights_pool) // 2))
    for name, abbr in names[:max_depts]:
        nd, nr = rng.randint(5, 9), rng.randint(2, 4)
        d = duties_pool[di : di + nd]
        r = rights_pool[ri : ri + nr]
        di += len(d)
        ri += len(r)
        if len(d) < 4 or len(r) < 1:
            break
        depts.append(Dept(name, abbr, rng.sample(POSITIONS, rng.randint(2, 4)), d, r))
    general = [
        "Настоящее Положение определяет задачи, функции, структуру, права и обязанности внутреннего аудита Общества.",
        "Блок внутреннего аудита является функциональным блоком, независимым по отношению к иным подразделениям Общества.",
        "Руководство блоком осуществляет Главный аудитор в соответствии с Уставом Общества.",
    ]
    return Doc(1, depts, general)


def mutate(rng: random.Random, doc: Doc, duties_pool: list[str], rights_pool: list[str], kinds: list[str]) -> tuple[Doc, list[Expectation]]:
    """Возвращает новую редакцию и таблицу ожиданий."""
    import copy

    new = copy.deepcopy(doc)
    new.edition = doc.edition + 1
    exp: list[Expectation] = []
    used = {_norm(t) for d in new.depts for t in d.duties + d.rights}
    fresh = [t for t in duties_pool if _norm(t) not in used]
    rng.shuffle(fresh)

    old_abbrs = {d.abbr for d in doc.depts}
    created: set[str] = set()

    def pick_dept(exclude: set[str] = frozenset(), only_old: bool = False):
        cands = [d for d in new.depts if d.abbr not in exclude and len(d.duties) >= 4 and (not only_old or d.abbr in old_abbrs)]
        return rng.choice(cands) if cands else None

    for kind in kinds:
        if kind == "lose":
            d = pick_dept()
            if not d:
                continue
            src = d.rights if d.rights and rng.random() < 0.4 else d.duties
            t = rng.choice(src)
            src.remove(t)
            exp.append(Expectation("lose", t, d.abbr))
        elif kind == "partial":
            d = pick_dept(only_old=True)  # хвост приклеивается к «до», значит департамент должен там быть
            if not d:
                continue
            # берём функцию и приклеиваем к ней хвост в «до», в «после» хвоста нет:
            # так частичная потеря гарантированно про конкретный кусок. Функция обязана быть
            # в обеих редакциях: предыдущие правки могли добавить в «после» новые пункты
            old_d = next(x for x in doc.depts if x.abbr == d.abbr)
            old_norms = {_norm(t): j for j, t in enumerate(old_d.duties)}
            shared = [i for i, t in enumerate(d.duties) if _norm(t) in old_norms and not t.rstrip(";.").endswith(tuple(x.strip() for x in TAILS))]
            if not shared:
                continue
            i = rng.choice(shared)
            base = d.duties[i].rstrip(";.")
            tail = rng.choice(TAILS)
            old_i = old_norms[_norm(d.duties[i])]
            old_d.duties[old_i] = base + tail + ";"
            d.duties[i] = base + ";"
            exp.append(Expectation("partial", base + tail + ";", d.abbr, note=tail.strip()))
        elif kind == "move":
            d = pick_dept()
            others = [x for x in new.depts if x is not d]
            if not d or not others:
                continue
            t = rng.choice(d.duties)
            d.duties.remove(t)
            to = rng.choice(others)
            to.duties.insert(rng.randrange(len(to.duties) + 1), t)
            exp.append(Expectation("move", t, d.abbr, to.abbr))
        elif kind == "dup":
            d = pick_dept()
            others = [x for x in new.depts if x is not d]
            if not d or not others:
                continue
            t = rng.choice(d.duties)
            to = rng.choice(others)
            if _norm(t) in {_norm(x) for x in to.duties}:
                continue
            to.duties.insert(rng.randrange(len(to.duties) + 1), t)
            exp.append(Expectation("dup", t, d.abbr, to.abbr))
        elif kind == "create":
            free = [n for n in DEPT_NAMES if n[1] not in {x.abbr for x in new.depts}]
            if not free or len(fresh) < 4:
                continue
            name, abbr = rng.choice(free)
            dts = [fresh.pop() for _ in range(rng.randint(4, 6))]
            new.depts.insert(rng.randrange(len(new.depts) + 1), Dept(name, abbr, rng.sample(POSITIONS, 3), dts, rng.sample(rights_pool, 2)))
            created.add(abbr)
            exp.append(Expectation("create", "", abbr))
        elif kind == "remove":
            # упразднять можно только департамент, который был в «до» и не создан в этом же сценарии
            cands = [d for d in new.depts if d.abbr in old_abbrs and d.abbr not in created]
            if len(new.depts) < 3 or not cands:
                continue
            d = rng.choice(cands)
            new.depts.remove(d)
            exp.append(Expectation("remove", "", d.abbr))
            # вместе с департаментом выпадают и обязанности, и права; все они честные потери,
            # если такой же функции нет у другого департамента
            for t in d.duties + d.rights:
                exp.append(Expectation("lose", t, d.abbr, note="вместе с департаментом"))
        elif kind == "paraphrase":
            d = pick_dept()
            if not d:
                continue
            for _ in range(10):
                i = rng.randrange(len(d.duties))
                t = d.duties[i]
                subs = [(a, b) for a, b in SYNONYMS if a in t]
                if subs:
                    a, b = rng.choice(subs)
                    d.duties[i] = t.replace(a, b, 1)
                    exp.append(Expectation("paraphrase", t, d.abbr, note=f"{a} -> {b}"))
                    break
        elif kind == "insert":
            d = pick_dept()
            if not d or not fresh:
                continue
            t = fresh.pop()
            d.duties.insert(0, t)
            exp.append(Expectation("insert", t, d.abbr))
    return new, exp


# ---------------------------------------------------------------- рендер


def render_paragraphs(doc: Doc) -> list[str]:
    p = [f"ПОЛОЖЕНИЕ О ВНУТРЕННЕМ АУДИТЕ АО «Компания» (редакция No{doc.edition})", "1. Общие положения"]
    for i, g in enumerate(doc.general, 1):
        p.append(f"1.{i}. {g}")
    p.append("2. Цели и задачи внутреннего аудита")
    p.append("2.1. Содействие Совету директоров и исполнительным органам Общества в повышении эффективности управления.")
    p.append("2.2. Задачи внутреннего аудита определяются с учетом имеющихся ресурсов и приоритетов деятельности Общества.")
    p.append("3. Структура и организация работы внутреннего аудита")
    p.append("3.1. Штатная численность подразделений, входящих в БВА, определяется штатным расписанием Общества.")
    p.append("3.2. БВА состоит из следующих структурных подразделений:")
    letters = "абвгдежзик"
    for i, d in enumerate(doc.depts):
        p.append(f"{letters[i]}. {d.name} ({d.abbr}).")
    p.append("3.3. Главному аудитору подчиняются работники БВА в соответствии со штатным расписанием в составе следующих должностей:")
    for i, d in enumerate(doc.depts):
        p.append(f"{letters[i]}. Директор {d.abbr}.")
    n = 4
    for d in doc.depts:
        p.append(f"3.{n}. Директору {d.abbr} подчиняются работники {d.abbr} в соответствии со штатным расписанием в составе следующих должностей:")
        for i, pos in enumerate(d.positions):
            p.append(f"{letters[i]}. {pos}.")
        n += 1
    p.append("4. Права и обязанности")
    p.append("Главный аудитор:")
    p.append("4.1. Организует работу БВА, осуществляя общее руководство и распределение обязанностей между работниками БВА:")
    p.append("4.1.1. организует работу по подготовке плана работ БВА и представляет его на утверждение Совету директоров;")
    p.append("4.1.2. утверждает программу и сроки проведения проверок Общества;")
    p.append("4.1.3. осуществляет другие полномочия в соответствии с решениями Совета директоров Общества.")
    k = 2
    for d in doc.depts:
        p.append(f"4.{k}. Директор {d.name.lower().replace('департамент', 'департамента')} (далее Директор {d.abbr}):")
        for i, t in enumerate(d.duties, 1):
            p.append(f"4.{k}.{i}. {t}")
        k += 1
        p.append(
            f"4.{k}. Директор {d.abbr} обязан обеспечить выполнение всех возложенных на {d.abbr} задач и функций в соответствии с настоящим Положением, а также имеет право:"
        )
        for i, t in enumerate(d.rights, 1):
            p.append(f"4.{k}.{i}. {t}")
        k += 1
    p.append("5. Ответственность")
    p.append("5.1. Работники БВА несут ответственность за надлежащее выполнение возложенных на них функций в соответствии с законодательством.")
    return p


def write_docx(doc: Doc, path: Path) -> Path:
    from docx import Document

    d = Document()
    for para in render_paragraphs(doc):
        d.add_paragraph(para)
    path.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(path))
    return path


def write_xlsx(doc: Doc, path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Структура"
    ws.append(["Подразделение", "Аббревиатура", "Должность", "Подчиняется", "Источник"])
    for d in doc.depts:
        ws.append([d.name, d.abbr, "", "БВА", "3.2"])
    for d in doc.depts:
        ws.append(["Блок внутреннего аудита", "", f"Директор {d.abbr}", "Главный аудитор", "3.3"])
        for pos in d.positions:
            ws.append([d.name, d.abbr, pos, f"Директор {d.abbr}", "3.x"])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


def docx_to_pdf(docx_path: Path, pdf_path: Path) -> bool:
    """Через Word, если он есть. Возвращает False, если конвертировать нечем."""
    import subprocess

    pdf_path.unlink(missing_ok=True)  # старый файл от прошлого прогона не должен сойти за результат
    ps = (
        "$w = New-Object -ComObject Word.Application; $w.Visible=$false; "
        f"$d = $w.Documents.Open('{docx_path.resolve()}', $false, $true); "
        f"$d.ExportAsFixedFormat('{pdf_path.resolve()}', 17); $d.Close($false); $w.Quit()"
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True, timeout=120)
        return pdf_path.exists() and pdf_path.stat().st_mtime >= docx_path.stat().st_mtime - 5
    except Exception:  # noqa: BLE001
        return False
