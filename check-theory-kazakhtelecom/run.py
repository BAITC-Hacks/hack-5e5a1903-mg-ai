"""Сквозной прогон теории на контрольных документах: редакция 8 против редакции 9.

    uv run run.py [--mode tfidf|openai|hybrid] [--verify] [--model gpt-4.1-mini]
                  [--tau 0.45] [--tau-global 0.6] [--tau-dup 0.6] [--high 0.85]

Ключ берется из окружения или из ../.env. Без ключа режим tfidf. Отчет в report.md.
Код возврата 0, если прошел весь лексический ярус эталона.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent
TZ = HERE.parent / "TZ"
BEFORE = TZ / "Положение_о_внутреннем_аудите_редакция_8_обезличено.docx"
AFTER = TZ / "Положение_о_внутреннем_аудите_редакция_9_обезличено.docx"
# если исходных docx организаторов рядом нет, берется тот же комплект в pdf из testdata
if not BEFORE.exists():
    BEFORE = HERE / "testdata" / "положение_ред8.pdf"
    AFTER = HERE / "testdata" / "положение_ред9.pdf"


def load_dotenv():
    for env in (HERE / ".env", HERE.parent / ".env"):
        if not env.exists() or os.environ.get("OPENAI_API_KEY"):
            continue
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("OPENAI_API_KEY=") and len(line) > 15:
                os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()


def ascii(s: str) -> str:
    return s.encode("ascii", "replace").decode()


def main():
    load_dotenv()
    from orgdiff.align import align
    from orgdiff.analyze import apply_verification, classify, conflicts_of_interest, duplicates, structure_changes, verify_duplicates
    from orgdiff.golden import score
    from orgdiff.load import load_clauses
    from orgdiff.similarity import Sim

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["tfidf", "openai", "hybrid"], default=None)
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--model", default="gpt-4.1-mini")
    ap.add_argument("--tau", type=float, default=0.45)
    ap.add_argument("--tau-global", type=float, default=0.6)
    ap.add_argument("--tau-dup", type=float, default=0.6)
    ap.add_argument("--high", type=float, default=0.85)
    ap.add_argument("--emb-floor", type=float, default=0.35)
    ap.add_argument("--out", default="report.md")
    args = ap.parse_args()

    A = load_clauses(BEFORE, "ред.8")
    B = load_clauses(AFTER, "ред.9")
    sim = Sim([c.text for c in A + B], mode=args.mode, emb_floor=args.emb_floor)

    calib = ""
    if sim.backend != "tfidf":
        raw = sim.raw_embedding_matrix([c.text for c in A[:200]], [c.text for c in B[:200]])
        off = raw[~np.eye(*raw.shape, dtype=bool)] if raw.shape[0] == raw.shape[1] else raw.ravel()
        calib = f"сырой косинус эмбеддингов: медиана {np.median(off):.2f}, p10 {np.percentile(off, 10):.2f}, p90 {np.percentile(off, 90):.2f}, max {off.max():.2f}"

    al = align([c.text for c in A], [c.text for c in B], sim, tau=args.tau)
    # с моделью кандидат на слияние можно брать с низкого порога, она подтвердит; без модели порог строже
    findings, cl = classify(al, A, B, sim, tau_global=args.tau_global, high=args.high, merge_tau=0.5 if args.verify else 0.75)
    structure = structure_changes(A, B)
    dups = duplicates(B, sim, tau_dup=args.tau_dup)
    coi = conflicts_of_interest(B)

    verifier = None
    dup_verdicts = []
    if args.verify:
        from orgdiff.verify import Verifier

        verifier = Verifier(model=args.model)
        apply_verification(findings, cl, verifier)
        dup_verdicts = verify_duplicates(dups, verifier)

    checks, stats = score(structure, findings, dups, coi)
    from orgdiff.golden import metrics as compute_metrics

    m = compute_metrics(structure, findings, dups, coi, llm_used=bool(verifier), A=A)
    kinds: dict[str, int] = {}
    for f in findings:
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
    by_a = {c.id: c for c in A}
    by_b = {c.id: c for c in B}

    rep = io.StringIO()
    w = rep.write
    w(
        f"# Отчет прототипа (backend={sim.backend}, verify={args.verify}, model={args.model if args.verify else '-'}, tau={args.tau}, tau_global={args.tau_global}, tau_dup={args.tau_dup}, high={args.high})\n\n"
    )
    w(f"Пунктов: ред.8 = {len(A)}, ред.9 = {len(B)}. Связей: {len(al.links)}, разрывов: {len(al.gaps_a)} / {len(al.gaps_b)}.\n\n")
    if calib:
        w(f"Калибровка: {calib}, пол = {args.emb_floor}. Эмбеддинг-вызовов {sim.embed_calls}, токенов {sim.embed_tokens}.\n\n")
    if verifier:
        w(
            f"Модель: {verifier.calls} вызовов, {verifier.tokens_in} вход / {verifier.tokens_out} выход, ~${verifier.cost_usd:.3f}, неподтвержденных цитат: {verifier.bad_quotes}.\n\n"
        )

    w("## Эталон\n\n")
    for tier in ("lexical", "semantic", "llm"):
        sub = [c for c in checks if c[3] == tier]
        if not sub:
            continue
        ok = sum(1 for c in sub if c[1])
        w(f"### {tier}: {ok} из {len(sub)}\n\n")
        for name, okk, detail, _ in sub:
            w(f"- [{'x' if okk else ' '}] {name}{('  — ' + detail) if detail and not okk else ''}\n")
        w("\n")
    w(f"Потерь всего {stats['lost_total']}, в разделе 5: {stats['lost_sec5']}, точность по разделу 5: {stats['precision_sec5']:.2f}\n\n")

    w("## Метрики по полной таблице истинных изменений\n\n")
    w(f"- Полнота: найдено {m['found']} из {m['total']} истинных изменений = **{m['recall'] * 100:.0f}%**\n")
    w(f"- Точность по потерям в разделах 4–5: {m['loss_tp']} верных из {m['loss_reported']} заявленных = **{m['precision_loss'] * 100:.0f}%**")
    w(f" (ложные: {m['loss_fp']})\n" if m["loss_fp"] else "\n")
    w(f"- F1 по потерям и полноте: **{m['f1'] * 100:.0f}%**\n\n")
    w("| Категория | Найдено | Всего |\n|---|---|---|\n")
    for cat, (f_, t_) in m["per_cat"].items():
        w(f"| {cat} | {f_} | {t_} |\n")
    w("\nПропущенные:\n\n")
    missed = [h for h in m["hits"] if not h[3]]
    for cat, key, desc, _ in missed:
        w(f"- {cat} {key}: {desc}\n")
    if not missed:
        w("- нет\n")
    w("\n")

    w("## Структура\n\n")
    for k in ("departments_created", "departments_removed", "departments_kept", "positions_created", "positions_removed"):
        w(f"- {k}: {structure[k]}\n")

    w("\n## Виды выводов\n\n")
    for k, v in sorted(kinds.items()):
        w(f"- {k}: {v}\n")

    def show_llm(f):
        if not f.llm:
            return ""
        d = f.llm
        q = d.get("quote_old") or d.get("quote_candidate") or ""
        return f"\n  LLM: {d.get('coverage', d.get('verdict', ''))}; {d.get('reason', '')[:160]}\n  цитата: «{q[:120]}» {'✓' if d.get('quotes_ok') else '✗ НЕ ПОДТВЕРЖДЕНА'}"

    w("\n## Потери (ред.8, нет пары в ред.9)\n\n")
    for f in findings:
        if f.kind == "lost":
            c = by_a[f.a_ids[0]]
            w(f"- **{f.a_ids[0]}** [{c.owner} | {c.modality}] {c.text[:140]}  \n  {f.note}{show_llm(f)}\n")

    w("\n## Частичные потери (пункт уцелел, часть функции пропала)\n\n")
    for f in findings:
        if f.kind == "partial_loss":
            a = by_a[f.a_ids[0]]
            b = by_b[f.b_ids[0]] if f.b_ids else None
            w(
                f"- **{a.id}** -> {b.id if b else '-'} sim={f.sim:.2f} [{a.owner} -> {b.owner if b else '-'}]\n  было: {a.text[:150]}\n  стало: {(b.text[:150] if b else '-')}\n  {f.note}{show_llm(f)}\n"
            )

    w("\n## Частично покрытые без вердикта модели\n\n")
    for f in findings:
        if f.kind == "partial":
            a, b = by_a[f.a_ids[0]], by_b[f.b_ids[0]]
            w(f"- **{a.id}** -> {b.id} sim={f.sim:.2f} [{a.owner} -> {b.owner}]\n  было: {a.text[:150]}\n  стало: {b.text[:150]}\n")

    w("\n## Снято с владельца (функция осталась только у другого)\n\n")
    for f in findings:
        if f.kind == "removed_from_owner":
            a = by_a[f.a_ids[0]]
            w(f"- **{a.id}** [{a.owner}] {a.text[:120]}  \n  {f.note}\n")

    w("\n## Добавлено владельцу (функция уже была у другого)\n\n")
    for f in findings:
        if f.kind == "added_to_owner":
            b = by_b[f.b_ids[0]]
            w(f"- **{b.id}** [{b.owner}] {b.text[:120]}  \n  {f.note}\n")

    w("\n## Переносы, слияния, покрытия и смена владельца\n\n")
    for f in findings:
        if f.kind in {"moved_to", "moved_from", "reassigned", "covered", "merged"}:
            w(f"- {f.kind} {f.a_ids} -> {f.b_ids} sim={f.sim:.2f} {f.note}\n")

    w("\n## Новые пункты (ред.9, нет пары в ред.8)\n\n")
    for f in findings:
        if f.kind == "new":
            c = by_b[f.b_ids[0]]
            w(f"- **{f.b_ids[0]}** [{c.owner} | {c.modality}] {c.text[:140]}  \n  {f.note}\n")

    w("\n## Дубли внутри ред.9, разделы 4-5 (кластеры с разными владельцами)\n\n")
    verdict_by_first = {g[0].id: d for g, d in dup_verdicts}
    for g in dups:
        w("- " + "; ".join(f"{c.id} [{(c.owner or '')[:40]}]" for c in g) + "\n")
        w(f"  «{g[0].text[:120]}»\n")
        d = verdict_by_first.get(g[0].id)
        if d:
            w(
                f"  LLM: **{d['verdict']}** {d['shared_function'][:100]} | {d['reason'][:160]} {'✓' if d['quotes_ok'] else '✗ цитаты не подтверждены'}\n"
            )

    w("\n## Конфликт интересов (по ключевым словам, ред.9)\n\n")
    for c in coi:
        w(f"- {c.id} [{c.owner}] {c.text[:160]}\n")

    w("\n## Изменённые пары с низкой близостью\n\n")
    for f in sorted((f for f in findings if f.kind in {"modified", "reassigned"}), key=lambda f: f.sim)[:20]:
        w(f"- {f.a_ids} <-> {f.b_ids} sim={f.sim:.2f} {f.note}{show_llm(f)}\n")

    (HERE / args.out).write_text(rep.getvalue(), encoding="utf-8")

    tiers = {t: [c for c in checks if c[3] == t] for t in ("lexical", "semantic", "llm")}
    summary = " | ".join(f"{t} {sum(1 for c in v if c[1])}/{len(v)}" for t, v in tiers.items() if v)
    print(f"backend={sim.backend} verify={args.verify} A={len(A)} B={len(B)} links={len(al.links)} gaps={len(al.gaps_a)}/{len(al.gaps_b)}")
    if calib:
        print("calib:", ascii(calib))
    if verifier:
        print(
            f"llm: calls={verifier.calls} in={verifier.tokens_in} out={verifier.tokens_out} cost=${verifier.cost_usd:.3f} bad_quotes={verifier.bad_quotes}"
        )
    print(f"kinds={kinds}")
    print(f"GOLDEN {summary} | lost_sec5={stats['lost_sec5']} precision_sec5={stats['precision_sec5']:.2f}")
    print(
        f"METRICS recall={m['recall'] * 100:.0f}% ({m['found']}/{m['total']}) precision_loss={m['precision_loss'] * 100:.0f}% ({m['loss_tp']}/{m['loss_reported']}) f1={m['f1'] * 100:.0f}% fp={[ascii(x) for x in m['loss_fp']]}"
    )
    for cat, key, _desc, ok in m["hits"]:
        if not ok:
            print(f"  MISSED {cat} {ascii(key)}")
    for name, okk, detail, tier in checks:
        print(("  PASS " if okk else "  FAIL ") + f"[{tier[:3]}] " + ascii(name) + (f"  {ascii(detail)}" if detail and not okk else ""))
    lex = tiers["lexical"]
    sys.exit(0 if all(c[1] for c in lex) else 1)


if __name__ == "__main__":
    main()
