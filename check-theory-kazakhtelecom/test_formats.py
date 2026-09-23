"""Проверка парсеров PDF и Excel против docx на контрольных документах.

У нас нет PDF и Excel от организаторов, поэтому тестовые файлы сделаны из тех же
контрольных docx: PDF настоящей конвертацией через Word, Excel как таблица оргструктуры
из раздела 3. Проверка честная в одном смысле: разбор чужого формата обязан дать те же
пункты и ту же структуру, что разбор docx. Если это так, конвейер за парсером работает
на любом из трёх форматов без изменений.

    uv run test_formats.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from orgdiff.load import load_clauses
from orgdiff.parse import parse
from orgdiff.structure import from_clauses, from_xlsx, structure_changes

HERE = Path(__file__).parent
TZ = HERE.parent / "TZ"
TD = HERE / "testdata"


def norm(s: str) -> str:
    s = re.sub(r"\s*-\s*", "-", s)  # экспорт в PDF расставляет пробелы вокруг дефисов
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".;:,")


def compare_pdf(n: int) -> bool:
    docx = TZ / f"Положение_о_внутреннем_аудите_редакция_{n}_обезличено.docx"
    pdf = TD / f"положение_ред{n}.pdf"
    A = parse(str(docx), "docx")
    B = load_clauses(str(pdf), "pdf")
    ida = {c.id: c for c in A}
    idb = {c.id: c for c in B}
    only_a = sorted(set(ida) - set(idb))
    only_b = sorted(set(idb) - set(ida))
    common = sorted(set(ida) & set(idb))
    text_eq = [i for i in common if norm(ida[i].text) == norm(idb[i].text)]
    owner_eq = [i for i in common if (ida[i].owner or "") == (idb[i].owner or "")]
    mod_eq = [i for i in common if ida[i].modality == idb[i].modality]
    numbered_a = [c.id for c in A if c.num]
    numbered_b = [c.id for c in B if c.num]
    print(f"--- PDF ред.{n}: docx {len(A)} пунктов, pdf {len(B)} пунктов ---")
    print(f"  нумерованных: docx {len(numbered_a)}, pdf {len(numbered_b)}")
    print(f"  общих id: {len(common)}, только в docx: {len(only_a)}, только в pdf: {len(only_b)}")
    print(f"  текст совпал: {len(text_eq)}/{len(common)} = {len(text_eq) / max(1, len(common)) * 100:.1f}%")
    print(f"  владелец совпал: {len(owner_eq)}/{len(common)}, модальность: {len(mod_eq)}/{len(common)}")
    for i in only_a[:8]:
        print(f"    только docx: {i} :: {ida[i].text[:70]}")
    for i in only_b[:8]:
        print(f"    только pdf : {i} :: {idb[i].text[:70]}")
    bad = [i for i in common if norm(ida[i].text) != norm(idb[i].text)]
    for i in bad[:6]:
        print(f"    текст разошёлся {i}:\n      docx: {ida[i].text[:100]}\n      pdf : {idb[i].text[:100]}")
    # критерий: все нумерованные пункты docx найдены в pdf с тем же текстом
    num_common = [i for i in numbered_a if i in idb]
    num_text_eq = [i for i in num_common if norm(ida[i].text) == norm(idb[i].text)]
    ok = len(num_common) >= 0.99 * len(numbered_a) and len(num_text_eq) >= 0.98 * len(num_common)
    print(
        f"  ИТОГ: нумерованные найдены {len(num_common)}/{len(numbered_a)}, текст совпал {len(num_text_eq)}/{len(num_common)} -> {'OK' if ok else 'FAIL'}"
    )
    return ok


def compare_xlsx(n: int) -> bool:
    docx = TZ / f"Положение_о_внутреннем_аудите_редакция_{n}_обезличено.docx"
    s_doc = from_clauses(parse(str(docx), "docx"))
    s_x = from_xlsx(TD / f"структура_ред{n}.xlsx")
    units_ok = s_doc.units == s_x.units
    pos_ok = s_doc.all_positions() == s_x.all_positions()
    print(
        f"--- XLSX ред.{n}: подразделений docx {len(s_doc.units)} / xlsx {len(s_x.units)}, должностей docx {len(s_doc.all_positions())} / xlsx {len(s_x.all_positions())} ---"
    )
    if not units_ok:
        print("    подразделения разошлись:", set(s_doc.units) ^ set(s_x.units))
    if not pos_ok:
        print("    должности разошлись:", s_doc.all_positions() ^ s_x.all_positions())
    print(f"  ИТОГ: {'OK' if units_ok and pos_ok else 'FAIL'}")
    return units_ok and pos_ok


def compare_structure_changes() -> bool:
    d8 = from_clauses(parse(str(TZ / "Положение_о_внутреннем_аудите_редакция_8_обезличено.docx"), "a"))
    d9 = from_clauses(parse(str(TZ / "Положение_о_внутреннем_аудите_редакция_9_обезличено.docx"), "b"))
    x8, x9 = from_xlsx(TD / "структура_ред8.xlsx"), from_xlsx(TD / "структура_ред9.xlsx")
    cd, cx = structure_changes(d8, d9), structure_changes(x8, x9)
    keys = ["departments_created", "departments_removed", "departments_kept", "positions_created", "positions_removed"]
    ok = all(cd[k] == cx[k] for k in keys)
    print("--- Структурные выводы docx против xlsx ---")
    for k in keys:
        print(f"  {k}: docx={cd[k]} xlsx={cx[k]} {'' if cd[k] == cx[k] else '<- РАЗОШЛОСЬ'}")
    print(f"  ИТОГ: {'OK' if ok else 'FAIL'}")
    return ok


if __name__ == "__main__":
    results = [compare_pdf(8), compare_pdf(9), compare_xlsx(8), compare_xlsx(9), compare_structure_changes()]
    print("\nВСЕГО:", sum(results), "из", len(results), "проверок прошли")
    sys.exit(0 if all(results) else 1)
