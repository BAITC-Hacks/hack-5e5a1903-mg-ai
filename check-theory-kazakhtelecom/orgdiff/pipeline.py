"""Единый конвейер: комплект «до» и комплект «после» любого формата -> результат в виде
словаря, пригодного для JSON, отчёта, docx и экрана. Всё, что показывает пользователь,
берётся отсюда и ничего не пересчитывает.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .align import align
from .analyze import (
    Finding,
    apply_verification,
    classify,
    conflicts_of_interest,
    duplicates,
    verify_coi,
    verify_duplicates,
)
from .load import load_set
from .parse import Clause
from .similarity import Sim
from .structure import structure_changes

KIND_TITLES = {
    "lost": "Потеря функции",
    "partial_loss": "Частичная потеря",
    "removed_from_owner": "Снято с владельца",
    "moved_to": "Перенос",
    "moved_from": "Перенос",
    "merged": "Слияние в общий блок",
    "covered": "Покрыто новым пунктом",
    "reassigned": "Смена владельца",
    "added_to_owner": "Добавлено владельцу",
    "new": "Новая функция",
    "partial_new": "Новая функция, есть похожая",
    "modified": "Изменён",
    "unchanged": "Без изменений",
    "partial": "Частичное покрытие, не проверено",
    "unlinked": "Пара разорвана",
}


def _clause(c: Clause) -> dict:
    return {"id": c.id, "text": c.text, "owner": c.owner, "modality": c.modality, "doc": c.doc, "section": c.section, "lead": c.is_lead}


def _finding(f: Finding, by_a: dict, by_b: dict) -> dict:
    return {
        "kind": f.kind,
        "title": KIND_TITLES.get(f.kind, f.kind),
        "a": [_clause(by_a[x]) for x in f.a_ids if x in by_a],
        "b": [_clause(by_b[y]) for y in f.b_ids if y in by_b],
        "sim": round(f.sim, 3),
        "note": f.note,
        "llm": f.llm,
        "candidate": _clause(by_b[f.candidate]) if f.candidate and f.candidate in by_b else None,
    }


def run_analysis(
    before: list[str | Path],
    after: list[str | Path],
    mode: str | None = None,
    verify: bool = True,
    model: str = "gpt-4.1-mini",
    tau: float = 0.45,
    tau_global: float = 0.6,
    tau_dup: float = 0.5,
    high: float = 0.85,
    golden: bool = False,
    progress=None,
    llm_owners: bool | None = None,
) -> dict:
    """llm_owners: владельцев блоков определяет модель по схеме (owners.LeadResolver).
    По умолчанию включено, когда есть ключ; без ключа всегда эвристика."""
    t0 = time.time()
    _p = progress or (lambda *_: None)

    def say(msg: str, replace: bool = False):
        try:
            _p(msg, replace)
        except TypeError:
            _p(msg)

    has_key = bool(os.environ.get("OPENAI_API_KEY"))
    if llm_owners is None:
        llm_owners = has_key
    resolver = None
    if llm_owners and has_key:
        from .owners import LeadResolver

        resolver = LeadResolver(model=model)
    say("Разбор документов" + (", владельцев блоков определяет модель" if resolver else ""))
    A, sa = load_set(before, "до", resolver=resolver)
    B, sb = load_set(after, "после", resolver=resolver)
    if not A or not B:
        raise ValueError("в каждом комплекте нужен хотя бы один docx или pdf с текстом положения")

    mode = mode or ("hybrid" if has_key else "tfidf")
    verify = verify and has_key
    say(f"Близость: режим {mode}")
    sim = Sim([c.text for c in A + B], mode=mode)

    say("Выравнивание пунктов")
    al = align([c.text for c in A], [c.text for c in B], sim, tau=tau)
    # с моделью кандидат на слияние берется с низкого порога, она его подтвердит; без модели порог строже
    findings, cl = classify(al, A, B, sim, tau_global=tau_global, high=high, merge_tau=0.5 if verify else 0.75)
    structure = structure_changes(sa, sb)
    dups = duplicates(B, sim, tau_dup=tau_dup if mode != "tfidf" else max(tau_dup, 0.6))
    coi = conflicts_of_interest(B)

    verifier = None
    dup_verdicts = []
    coi_verdicts: dict[str, dict] = {}
    if verify:
        from .verify import Verifier

        stage = {"name": f"Проверка спорных мест моделью {model}"}
        say(stage["name"])

        def tick(n: int):
            # строка прогресса заменяет предыдущую, если колбэк это умеет (см. app.py)
            say(f"{stage['name']} · обращений к модели: {n}", True)

        verifier = Verifier(model=model, on_call=tick)
        apply_verification(findings, cl, verifier)
        stage["name"] = f"Подтверждение дублей, кластеров: {len(dups)}"
        say(stage["name"])
        dup_verdicts = verify_duplicates(dups, verifier)
        if coi:
            say(f"Оценка пунктов о конфликте интересов: {len(coi)}")
            coi_verdicts = verify_coi(coi, verifier)

    by_a = {c.id: c for c in A}
    by_b = {c.id: c for c in B}
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.kind] = counts.get(f.kind, 0) + 1

    verdict_by_first = {g[0].id: d for g, d in dup_verdicts}
    dup_out = []
    for g in dups:
        d = verdict_by_first.get(g[0].id)
        if d and d.get("verdict") == "different":
            continue  # модель отклонила кластер: совпадение только по словам
        dup_out.append({"clauses": [_clause(c) for c in g], "verdict": d})

    result = {
        "meta": {
            "before": [Path(p).name for p in before],
            "after": [Path(p).name for p in after],
            "mode": sim.backend,
            "verify": verify,
            "model": model if verify else None,
            "llm_calls": (verifier.calls if verifier else 0) + (resolver.calls if resolver else 0),
            "llm_owners": resolver is not None,
            "llm_cost_usd": round(verifier.cost_usd, 3) if verifier else 0.0,
            "bad_quotes_blocked": verifier.bad_quotes if verifier else 0,
            "embed_tokens": sim.embed_tokens,
            "clauses_before": len(A),
            "clauses_after": len(B),
            "seconds": round(time.time() - t0, 1),
        },
        "structure": structure,
        "findings": [_finding(f, by_a, by_b) for f in findings],
        "duplicates": dup_out,
        "coi": [{**_clause(c), "verdict": coi_verdicts.get(c.id)} for c in coi],
        "counts": counts,
        "clauses": {"before": [_clause(c) for c in A], "after": [_clause(c) for c in B]},
    }

    if golden:
        from .golden import metrics, score

        checks, stats = score(structure, findings, dups, coi)
        m = metrics(structure, findings, dups, coi, llm_used=bool(verifier), A=A)
        result["golden"] = {
            "checks": [{"name": n, "ok": ok, "detail": d, "tier": t} for n, ok, d, t in checks],
            "recall": m["recall"],
            "precision_loss": m["precision_loss"],
            "f1": m["f1"],
            "found": m["found"],
            "total": m["total"],
            "loss_fp": m["loss_fp"],
            "missed": [f"{c} {k}" for c, k, _, ok in m["hits"] if not ok],
        }
    say("Готово")
    return result
