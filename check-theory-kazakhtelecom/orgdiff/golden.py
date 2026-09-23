"""Эталон: что мы заранее знаем об изменениях между редакциями 8 и 9.

Две части:
1. Проверки в три яруса (lexical / semantic / llm), как раньше.
2. Полная таблица истинных изменений в разделах 3-5 и метрики полноты и точности по ней.
   Таблица собрана чтением diff двух редакций руками и перепроверена на прогонах.

Важная оговорка: это контрольный комплект, на котором прототип и настраивался.
Проценты на нем это верхняя оценка. На чужом комплекте будет ниже.
"""

from __future__ import annotations

from .analyze import Finding
from .parse import Clause

EXPECTED_DEPT_CREATED = {"ДИТААД", "ДОА"}
EXPECTED_DEPT_KEPT = {"ДНМ", "ДККМ"}
EXPECTED_POSITION_REMOVED = "Директор направления внутреннего аудита"

EXPECTED_LOST = ["5.6.2", "5.6.3", "5.7.2"]
EXPECTED_LOST_OR_PARTIAL = ["5.7.1"]
EXPECTED_NOT_LOST = ["5.7.3", "5.7.4", "5.7.5"]
EXPECTED_MOVED = ("5.4.4", "5.3.3")
EXPECTED_MODIFIED = ("5.5.5", "5.5.3")
EXPECTED_PARTIAL_LOSS = [
    ("5.7.1", "предложения по программам гарантий и консультаций"),
    ("5.6.7", "сужение с работников БВА до работников департамента"),
    ("5.3.4.б", "контроль сроков выполнения графика"),
    ("5.3.4.а", "организация работы проектной команды по проверке"),
    ("5.5.5", "ежеквартально и по итогам года"),
]
MAX_PARTIAL_LOSS_SEC45 = 10
DUP_PAIRS_LEXICAL = [("5.3.6", "5.4.3"), ("5.3.12", "5.4.9"), ("5.3.8", "5.4.5")]
DUP_PAIRS_SEMANTIC = [("5.3.6", "5.5.8"), ("5.3.7", "5.5.5")]
EXPECTED_COI = "4.4"

# ---------------------------------------------------------------------------
# Полная таблица истинных изменений. category, ключ, пояснение.
# ---------------------------------------------------------------------------
GROUND_TRUTH = [
    ("structure", "created:ДИТААД", "создан ДИТААД"),
    ("structure", "created:ДОА", "создан ДОА"),
    ("structure", "kept:ДНМ", "сохранен ДНМ"),
    ("structure", "kept:ДККМ", "сохранен ДККМ"),
    ("structure", "position_removed", "упразднен Директор направления внутреннего аудита"),
    ("lost", "5.6.2", "ДККМ: формировать группы контроля качества"),
    ("lost", "5.6.3", "ДККМ: предложения по внешней оценке БВА"),
    ("lost", "5.7.2", "ДНМ: доводить результаты консультаций"),
    ("partial_loss", "5.7.1", "ДНМ: предложения по программам гарантий и консультаций"),
    ("partial_loss", "5.5.5", "ДККМ: периодичность отчетов"),
    ("partial_loss", "5.6.7", "ДККМ: сужение круга работников"),
    ("partial_loss", "5.3.4.а", "организация работы проектной команды"),
    ("partial_loss", "5.3.4.б", "контроль сроков графика"),
    ("partial_loss", "5.7.5", "ДНМ: адресат и круг работников"),
    # Исправлено после прогона: эти три функции ДККМ не снял, они ушли в общий блок 5.3,
    # владелец которого "Директоры департаментов", а директор ДККМ один из них.
    ("merged", "5.5.4", "ДККМ: анализ результатов непрерывного аудита, теперь в общем блоке директоров 5.3.8"),
    ("merged", "5.5.8", "ДККМ: предложения по профуровню, теперь в общем блоке 5.3.9"),
    ("merged", "5.5.10", "ДККМ: предложения в план работ, теперь в общем блоке 5.3.3"),
    ("transfer", "5.4.4->5.3.3", "взаимодействие с субъектами СВК от ДНМ ко всем директорам"),
    ("not_lost", "5.7.3", "ДНМ: переписка, влилось в общее 5.6"),
    ("not_lost", "5.7.4", "ДНМ: заседания, влилось в общее 5.6"),
    # 5.3.1 редакции 9 либо новый пункт, либо результат переработки 5.3.1 упраздненной роли:
    # выравнивание связывает их с близостью 0.46, и оба прочтения защитимы
    ("new_or_reworked", "5.3.1", "стратегическое управление аудитом по направлениям"),
    ("new", "5.3.2.а", "зона ДИТААД"),
    ("new", "5.3.2.б", "зона ДОА"),
    ("new", "4.4.а", "информирование о конфликте при совмещении"),
    ("new", "4.4.б", "декларация о совмещении"),
    ("dup", "5.3.6~5.4.3", "запрос информации у руководителей"),
    ("dup", "5.3.6~5.5.8", "запрос информации у руководителей, ДККМ"),
    ("dup", "5.3.12~5.4.9", "участие в разработке ВНД"),
    ("dup", "5.3.8~5.4.5", "анализ результатов непрерывного аудита"),
    ("dup", "5.3.7~5.5.5", "контроль устранения недостатков"),
    ("coi", "4.4", "участие Главного аудитора в органах управления ДЗО"),
]
# пункт 5.5.3 редакции 8 это одна точка с запятой, дефект исходника. Его отчет как "потерю"
# не считаем ни попаданием, ни ошибкой.
IGNORE_AS_FP = {"5.5.3"}
LOSS_KINDS = {"lost", "partial_loss", "removed_from_owner"}


def _starts(cid: str, num: str) -> bool:
    return cid == num or cid.startswith(num + ".")


def _same_cluster(dups, x, y):
    for g in dups:
        ids = [c.id for c in g]
        if any(_starts(i, x) for i in ids) and any(_starts(i, y) for i in ids):
            return True
    return False


def metrics(structure: dict, findings: list[Finding], dups: list[list[Clause]], coi: list[Clause], llm_used: bool, A: list[Clause] | None = None):
    """Полнота по таблице истинных изменений и точность по классам потерь."""
    leads = {c.id for c in (A or []) if c.is_lead}
    hits = []
    for cat, key, desc in GROUND_TRUTH:
        ok = False
        if cat == "structure":
            if key.startswith("created:"):
                ok = key.split(":")[1] in structure["departments_created"]
            elif key.startswith("kept:"):
                ok = key.split(":")[1] in structure["departments_kept"]
            else:
                ok = EXPECTED_POSITION_REMOVED in structure["positions_removed"]
        elif cat == "lost":
            ok = any(f.kind == "lost" and _starts(f.a_ids[0], key) for f in findings)
        elif cat == "partial_loss":
            kinds = {"partial_loss"} if llm_used else {"partial", "partial_loss", "modified", "reassigned"}
            ok = any(f.kind in kinds and f.a_ids and _starts(f.a_ids[0], key) for f in findings)
        elif cat == "merged":
            ok = any(f.kind in {"merged", "covered"} and f.a_ids and _starts(f.a_ids[0], key) for f in findings)
        elif cat == "transfer":
            a, b = key.split("->")
            ok = any(
                f.kind in {"moved_to", "moved_from", "merged", "reassigned", "covered"}
                and any(_starts(x, a) for x in f.a_ids)
                and any(_starts(y, b) for y in f.b_ids)
                for f in findings
            )
        elif cat == "not_lost":
            ok = not any(f.kind == "lost" and _starts(f.a_ids[0], key) for f in findings)
        elif cat == "new":
            ok = any(f.kind in {"new", "added_to_owner", "partial_new"} and f.b_ids and _starts(f.b_ids[0], key) for f in findings)
        elif cat == "new_or_reworked":
            ok = any(
                f.kind in {"new", "added_to_owner", "partial_new", "partial_loss", "modified", "reassigned"} and any(_starts(y, key) for y in f.b_ids)
                for f in findings
            )
        elif cat == "dup":
            x, y = key.split("~")
            ok = _same_cluster(dups, x, y)
        elif cat == "coi":
            ok = any(_starts(c.id, key) for c in coi)
        hits.append((cat, key, desc, ok))

    total = len(hits)
    found = sum(1 for h in hits if h[3])
    per_cat: dict[str, list[int]] = {}
    for cat, _, _, ok in hits:
        per_cat.setdefault(cat, [0, 0])
        per_cat[cat][1] += 1
        per_cat[cat][0] += int(ok)

    truth_loss_keys = {key for cat, key, _ in GROUND_TRUTH if cat in {"lost", "partial_loss", "removed_from_owner"}}

    # потеря функции это потеря пункта-функции; заголовок блока функцией не является,
    # его исчезновение это структура, и она учтена отдельно
    def func_ids(f):
        return [x for x in f.a_ids if x not in leads]

    reported = [f for f in findings if f.kind in LOSS_KINDS and func_ids(f) and func_ids(f)[0].startswith(("4.", "5."))]
    reported = [f for f in reported if not any(_starts(x, ig) for x in func_ids(f) for ig in IGNORE_AS_FP)]
    # функция, снятая с упраздненной должности, это следствие упразднения, а оно уже
    # учтено в структуре. Ложным срабатыванием не считаем, но и попаданием тоже.
    removed_positions = set(structure["positions_removed"])
    reported = [
        f
        for f in reported
        if not (f.kind == "removed_from_owner" and (f.note.split("«")[1].split("»")[0] if "«" in f.note else "") in removed_positions)
    ]
    tp = [f for f in reported if any(_starts(x, k) for x in func_ids(f) for k in truth_loss_keys)]
    fp = [f for f in reported if f not in tp]
    precision = len(tp) / len(reported) if reported else 0.0
    recall = found / total
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "hits": hits,
        "per_cat": per_cat,
        "found": found,
        "total": total,
        "recall": recall,
        "loss_reported": len(reported),
        "loss_tp": len(tp),
        "loss_fp": [func_ids(f)[0] for f in fp],
        "precision_loss": precision,
        "f1": f1,
    }


def score(structure: dict, findings: list[Finding], dups: list[list[Clause]], coi: list[Clause]):
    checks: list[tuple[str, bool, str, str]] = []

    def add(name, ok, detail="", tier="lexical"):
        checks.append((name, bool(ok), detail, tier))

    created = set(structure["departments_created"])
    add("created ДИТААД, ДОА", created == EXPECTED_DEPT_CREATED, str(sorted(created)))
    add("kept ДНМ, ДККМ", EXPECTED_DEPT_KEPT <= set(structure["departments_kept"]))
    add("position removed: Директор направления", EXPECTED_POSITION_REMOVED in structure["positions_removed"])

    lost_ids = [f.a_ids[0] for f in findings if f.kind == "lost"]
    partial_ids = [f.a_ids[0] for f in findings if f.kind in {"partial", "partial_loss"}]
    for num in EXPECTED_LOST:
        add(f"lost {num}", any(_starts(i, num) for i in lost_ids))
    for num in EXPECTED_LOST_OR_PARTIAL:
        add(f"lost or partial {num}", any(_starts(i, num) for i in lost_ids + partial_ids))
    for num in EXPECTED_NOT_LOST:
        hit = any(_starts(i, num) for i in lost_ids)
        add(f"NOT lost {num}", not hit, "помечен потерянным" if hit else "")

    moved = [f for f in findings if f.kind in {"moved_to", "moved_from", "reassigned", "partial", "merged", "covered"}]
    a_num, b_num = EXPECTED_MOVED
    add(
        f"moved {a_num} -> {b_num}",
        any(any(_starts(x, a_num) for x in f.a_ids) and any(_starts(y, b_num) for y in f.b_ids) for f in moved),
    )
    a_num, b_num = EXPECTED_MODIFIED
    add(
        f"linked {a_num} <-> {b_num}",
        any(
            any(_starts(x, a_num) for x in f.a_ids) and any(_starts(y, b_num) for y in f.b_ids)
            for f in findings
            if f.kind in {"modified", "unchanged", "reassigned", "partial_loss"}
        ),
    )
    for x, y in DUP_PAIRS_LEXICAL:
        add(f"dup {x} ~ {y}", _same_cluster(dups, x, y))
    for x, y in DUP_PAIRS_SEMANTIC:
        add(f"dup {x} ~ {y}", _same_cluster(dups, x, y), tier="semantic")
    add("coi 4.4", any(_starts(c.id, EXPECTED_COI) for c in coi), str([c.id for c in coi]))

    if any(f.llm for f in findings):
        partial_loss = [f for f in findings if f.kind == "partial_loss"]
        for num, why in EXPECTED_PARTIAL_LOSS:
            add(f"llm partial loss {num} ({why})", any(any(_starts(x, num) for x in f.a_ids) for f in partial_loss), tier="llm")
        confirmed_lost = [f.a_ids[0] for f in findings if f.kind == "lost" and f.llm]
        for num in EXPECTED_LOST:
            add(f"llm still lost after review {num}", any(_starts(i, num) for i in confirmed_lost), tier="llm")
        changed = [f for f in findings if f.llm and f.kind in {"partial_loss", "covered"}]
        add("llm bad quotes never changed a verdict", all(f.llm.get("quotes_ok") for f in changed), tier="llm")
        n45 = sum(1 for f in partial_loss if f.a_ids[0].startswith(("4.", "5.")))
        add(f"llm partial losses in sec 4-5 <= {MAX_PARTIAL_LOSS_SEC45}", n45 <= MAX_PARTIAL_LOSS_SEC45, f"{n45}", tier="llm")

    lost_sec5 = [i for i in lost_ids if i.startswith("5.")]
    expected_hits = [i for i in lost_sec5 if any(_starts(i, n) for n in EXPECTED_LOST + EXPECTED_LOST_OR_PARTIAL)]
    precision = len(expected_hits) / len(lost_sec5) if lost_sec5 else 0.0
    return checks, {"lost_total": len(lost_ids), "lost_sec5": len(lost_sec5), "precision_sec5": precision}
