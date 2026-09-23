"""Форматы входа: pdf и xlsx дают ту же структуру, что и docx, если docx доступен."""

from pathlib import Path

import pytest

from orgdiff.structure import from_clauses, from_xlsx, structure_changes

TZ = Path(__file__).parent.parent.parent / "TZ"


def test_xlsx_structure_matches_structure_from_text(clauses_before, before_xlsx, clauses_after, after_xlsx):
    for clauses, xlsx in ((clauses_before, before_xlsx), (clauses_after, after_xlsx)):
        s_text, s_x = from_clauses(clauses), from_xlsx(xlsx)
        assert s_text.units == s_x.units
        assert s_text.all_positions() == s_x.all_positions()


def test_structure_changes_are_identical_from_text_and_from_xlsx(clauses_before, clauses_after, before_xlsx, after_xlsx):
    from_text = structure_changes(from_clauses(clauses_before), from_clauses(clauses_after))
    from_x = structure_changes(from_xlsx(before_xlsx), from_xlsx(after_xlsx))
    for k in ("departments_created", "departments_removed", "departments_kept", "positions_created", "positions_removed"):
        assert from_text[k] == from_x[k]
    assert from_x["departments_created"] == ["ДИТААД", "ДОА"]
    assert from_x["positions_removed"] == ["Директор направления внутреннего аудита"]


@pytest.mark.skipif(not (TZ / "Положение_о_внутреннем_аудите_редакция_9_обезличено.docx").exists(), reason="исходный docx организаторов недоступен")
def test_pdf_parse_matches_docx_parse():
    import re

    from orgdiff.load import load_clauses
    from orgdiff.parse import parse

    def norm(s):
        s = re.sub(r"\s*-\s*", "-", s)
        return re.sub(r"\s+", " ", s).strip().lower().rstrip(".;:,")

    docx = parse(str(TZ / "Положение_о_внутреннем_аудите_редакция_9_обезличено.docx"), "docx")
    pdf = load_clauses(Path(__file__).parent.parent / "testdata" / "положение_ред9.pdf", "pdf")
    ida = {c.id: c for c in docx if c.num}
    idb = {c.id: c for c in pdf}
    common = [i for i in ida if i in idb]
    assert len(common) >= 0.99 * len(ida)
    same = sum(1 for i in common if norm(ida[i].text) == norm(idb[i].text))
    assert same >= 0.98 * len(common)


def test_unsupported_format_is_rejected(tmp_path):
    from orgdiff.load import load_clauses

    p = tmp_path / "x.txt"
    p.write_text("1. text", encoding="utf-8")
    with pytest.raises(ValueError):
        load_clauses(p, "t")
