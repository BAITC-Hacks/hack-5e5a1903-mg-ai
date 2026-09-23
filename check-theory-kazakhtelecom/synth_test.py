"""Прогон конвейера по синтетическим парам документов с известными изменениями.

    uv run synth_test.py [--n 20] [--seed 1] [--mode hybrid] [--verify] [--pdf-every 5]

Для каждого сценария: собирается положение «до» с 3-6 департаментами, в «после» вносятся
3-6 изменений известных типов, обе редакции пишутся в docx (и xlsx для структуры, и pdf
для каждого pdf-every сценария), конвейер прогоняется, и каждое ожидание сверяется
с выводами. Печатается таблица по сценариям и сводные полнота и точность.
"""

from __future__ import annotations

import argparse
import io
import random
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
OUT = HERE / ".cache" / "synth"


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".;:,")


def main():
    from run import load_dotenv

    load_dotenv()
    from orgdiff.pipeline import run_analysis
    from orgdiff.synth import docx_to_pdf, load_pool, make_doc, mutate, write_docx, write_xlsx

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--mode", default="hybrid")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--pdf-every", type=int, default=4)
    ap.add_argument("--out", default=str(HERE / "reports" / "synth.md"))
    args = ap.parse_args()

    duties, rights = load_pool(HERE.parent / "TZ")
    print(f"пул: {len(duties)} обязанностей, {len(rights)} прав")
    KINDS = ["lose", "partial", "move", "dup", "create", "remove", "paraphrase", "insert"]

    rows = []
    totals = {"expected": 0, "found": 0, "false_losses": 0, "reported_losses": 0}
    per_kind: dict[str, list[int]] = {}
    lines = [f"# Синтетический прогон: n={args.n}, seed={args.seed}, mode={args.mode}, verify={args.verify}\n"]

    for s in range(args.n):
        rng = random.Random(args.seed * 1000 + s)
        base = make_doc(rng, duties, rights, rng.randint(3, 6))
        kinds = rng.sample(KINDS, rng.randint(3, 6))
        new, exp = mutate(rng, base, duties, rights, kinds)
        d = OUT / f"seed{args.seed}" / f"s{s:02d}"  # своя папка на зерно: параллельные прогоны не пишут в одни файлы
        a_docx, b_docx = write_docx(base, d / "before.docx"), write_docx(new, d / "after.docx")
        a_x, b_x = write_xlsx(base, d / "before.xlsx"), write_xlsx(new, d / "after.xlsx")
        before, after = [a_docx, a_x], [b_docx, b_x]
        fmt = "docx+xlsx"
        if args.pdf_every and s % args.pdf_every == 0:
            if docx_to_pdf(a_docx, d / "before.pdf") and docx_to_pdf(b_docx, d / "after.pdf"):
                before, after = [d / "before.pdf", a_x], [d / "after.pdf", b_x]
                fmt = "pdf+xlsx"

        t0 = time.time()
        try:
            r = run_analysis(before, after, mode=args.mode, verify=args.verify)
        except Exception as e:  # noqa: BLE001
            rows.append((s, fmt, kinds, "ОШИБКА " + str(e)[:80], 0, 0))
            lines.append(f"## s{s:02d} ({fmt}) ОШИБКА: {e}\n")
            continue
        dt = time.time() - t0

        findings = r["findings"]
        st = r["structure"]

        def is_lead(c):
            return bool(c.get("lead")) or (c.get("modality") == "none" and c["text"].rstrip().endswith(":"))

        def a_text(f):
            for c in f["a"]:
                if not is_lead(c):
                    return c["text"]
            return ""  # только заголовки: функцией не является

        def owner_abbr(c):
            o = (c or {}).get("owner") or ""
            m = re.search(r"\b([А-ЯЁ]{2,6})\b", o)
            return m.group(1) if m else o

        results = []
        for e in exp:
            ok, how = False, ""
            if e.kind == "create":
                ok = e.dept in st["departments_created"]
            elif e.kind == "remove":
                ok = e.dept in st["departments_removed"]
            elif e.kind in {"lose", "lose_or_merged"}:
                accept = {"lost", "removed_from_owner"} | ({"merged", "moved_to", "covered"} if e.kind == "lose_or_merged" else set())
                if not args.verify:
                    accept |= {"partial"}  # без модели «частично покрыто» это очередь на проверку, а не пропуск
                for f in findings:
                    if f["kind"] in accept and norm(a_text(f)) == norm(e.text):
                        ok, how = True, f["kind"]
                        break
                if not ok:
                    for f in findings:
                        if norm(a_text(f)) == norm(e.text):
                            how = "найден как " + f["kind"]
            elif e.kind == "partial":
                accept = {"partial_loss"} if args.verify else {"partial", "partial_loss", "modified", "reassigned"}
                for f in findings:
                    if f["kind"] in accept and norm(a_text(f)) == norm(e.text):
                        ok, how = True, f["kind"]
                        break
                if not ok:
                    for f in findings:
                        if norm(a_text(f)) == norm(e.text):
                            how = "найден как " + f["kind"]
            elif e.kind == "move":
                for f in findings:
                    if norm(a_text(f)) == norm(e.text) and f["kind"] in {"moved_to", "removed_from_owner", "merged", "reassigned", "covered"}:
                        b = f["b"][0] if f["b"] else None
                        if b and owner_abbr(b) == e.dept_to:
                            ok, how = True, f["kind"]
                            break
                        how = f"{f['kind']} -> {owner_abbr(b)}"
                if not ok:
                    # перенос виден и с другой стороны: у получателя появилась функция, которой у него не было
                    for f in findings:
                        b = f["b"][0] if f["b"] else None
                        if (
                            b
                            and f["kind"] in {"added_to_owner", "moved_from", "new", "partial_new"}
                            and norm(b["text"]) == norm(e.text)
                            and owner_abbr(b) == e.dept_to
                        ):
                            ok, how = True, f["kind"] + " у получателя"
                            break
            elif e.kind == "dup":
                for g in r["duplicates"]:
                    owners = {owner_abbr(c) for c in g["clauses"]}
                    texts = {norm(c["text"]) for c in g["clauses"]}
                    if norm(e.text) in texts and {e.dept, e.dept_to} <= owners:
                        ok, how = True, (g.get("verdict") or {}).get("verdict", "")
                        break
            elif e.kind == "paraphrase":
                ok = not any(f["kind"] in {"lost", "removed_from_owner"} and norm(a_text(f)) == norm(e.text) for f in findings)
                how = "не потеря" if ok else "ЛОЖНАЯ ПОТЕРЯ"
            elif e.kind == "insert":
                # вставка сдвигает нумерацию; проверяем, что остальные функции департамента не стали потерями
                dept_losses = [f for f in findings if f["kind"] == "lost" and owner_abbr(f["a"][0]) == e.dept and norm(a_text(f)) != norm(e.text)]
                ok = not dept_losses
                how = "нумерация не сломала" if ok else f"ложных потерь {len(dept_losses)}"
            results.append((e, ok, how))
            per_kind.setdefault(e.kind, [0, 0])
            per_kind[e.kind][1] += 1
            per_kind[e.kind][0] += int(ok)

        # функция упразднённого департамента, которая есть ещё у кого-то, честно уходит в слияние,
        # а не в потерю; такие ожидания снимаем, иначе генератор спорит с правильным ответом
        all_new_texts = {norm(t) for d in new.depts for t in d.duties + d.rights}
        for e in exp:
            if e.kind == "lose" and e.note == "вместе с департаментом" and norm(e.text) in all_new_texts:
                e.kind = "lose_or_merged"
        expected_loss_texts = {norm(e.text) for e in exp if e.kind in {"lose", "move", "lose_or_merged"}}
        reported = [
            f
            for f in findings
            if f["kind"] in {"lost", "removed_from_owner", "partial_loss"} and f["a"] and f["a"][0]["section"] == "4" and a_text(f)
        ]
        false_losses = [
            f
            for f in reported
            if norm(a_text(f)) not in expected_loss_texts and norm(a_text(f)) not in {norm(e.text) for e in exp if e.kind == "partial"}
        ]

        n_ok = sum(1 for _, ok, _ in results)
        n_found = sum(1 for _, ok, _ in results if ok)
        totals["expected"] += n_ok
        totals["found"] += n_found
        totals["false_losses"] += len(false_losses)
        totals["reported_losses"] += len(reported)
        rows.append((s, fmt, kinds, f"{n_found}/{n_ok}", len(false_losses), round(dt, 1)))

        lines.append(
            f"## s{s:02d} ({fmt}, {len(base.depts)} деп., изменения: {', '.join(kinds)}) — найдено {n_found}/{n_ok}, ложных потерь {len(false_losses)}, {dt:.0f}s\n"
        )
        for e, ok, how in results:
            lines.append(
                f"- [{'x' if ok else ' '}] {e.kind} {e.dept}{('->' + e.dept_to) if e.dept_to else ''}: {e.text[:90]} {('· ' + how) if how else ''}"
            )
        for f in false_losses:
            lines.append(f"- ЛОЖНАЯ {f['kind']}: {a_text(f)[:90]} [{owner_abbr(f['a'][0])}]")
        lines.append("")

    recall = totals["found"] / totals["expected"] if totals["expected"] else 0
    precision = 1 - totals["false_losses"] / totals["reported_losses"] if totals["reported_losses"] else 1.0
    summary = [
        f"\n## Итого: ожиданий {totals['expected']}, найдено {totals['found']} = полнота {recall * 100:.0f}%; заявлено потерь {totals['reported_losses']}, ложных {totals['false_losses']} = точность {precision * 100:.0f}%\n",
        "| Тип изменения | Найдено | Всего |",
        "|---|---|---|",
    ]
    for k, (f_, t_) in per_kind.items():
        summary.append(f"| {k} | {f_} | {t_} |")
    Path(args.out).parent.mkdir(exist_ok=True)
    io.open(args.out, "w", encoding="utf-8").write("\n".join(lines + summary))

    print(f"{'sc':>3} {'формат':10} {'найдено':>8} {'ложн':>5} {'сек':>5}  изменения")
    for s, fmt, kinds, res, fl, dt in rows:
        print(f"{s:>3} {fmt:10} {res:>8} {fl:>5} {dt:>5}  {','.join(kinds)}")
    print(
        f"\nИТОГО: полнота {recall * 100:.0f}% ({totals['found']}/{totals['expected']}), точность по потерям {precision * 100:.0f}% (ложных {totals['false_losses']} из {totals['reported_losses']})"
    )
    for k, (f_, t_) in per_kind.items():
        print(f"  {k:11} {f_}/{t_}")
    print("отчёт:", args.out)
    sys.exit(0 if recall >= 0.9 and precision >= 0.85 else 1)


if __name__ == "__main__":
    main()
