"""Документы, которых конвейер не видел: синтетические положения с известными изменениями."""

import random
import re

import pytest

from orgdiff.pipeline import run_analysis
from orgdiff.synth import FALLBACK_DUTIES, load_pool, make_doc, mutate, write_docx, write_xlsx


def norm(s):
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".;:,")


def _owner_abbr(c):
    m = re.search(r"\b([А-ЯЁ]{2,6})\b", (c or {}).get("owner") or "")
    return m.group(1) if m else ""


@pytest.fixture(scope="module")
def pool():
    duties, rights = load_pool(None)
    assert duties == [d.strip() for d in FALLBACK_DUTIES] or len(duties) >= len(FALLBACK_DUTIES) - 2
    assert len(rights) >= 8
    return duties, rights


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_known_changes_are_found_without_false_losses(tmp_path, pool, seed):
    duties, rights = pool
    rng = random.Random(seed)
    base = make_doc(rng, duties, rights, 4)
    new, exp = mutate(rng, base, duties, rights, ["lose", "create", "move", "dup", "paraphrase", "insert"])
    d = tmp_path / f"s{seed}"
    before = [write_docx(base, d / "before.docx"), write_xlsx(base, d / "before.xlsx")]
    after = [write_docx(new, d / "after.docx"), write_xlsx(new, d / "after.xlsx")]
    r = run_analysis(before, after, mode="tfidf", verify=False, llm_owners=False)

    findings = r["findings"]
    st = r["structure"]
    kinds = {e.kind for e in exp}
    if "create" in kinds:
        assert all(e.dept in st["departments_created"] for e in exp if e.kind == "create")
    for e in exp:
        if e.kind == "lose":
            hits = [f for f in findings if f["a"] and norm(f["a"][0]["text"]) == norm(e.text)]
            assert hits and hits[0]["kind"] in {"lost", "removed_from_owner", "partial"}, f"потеря не найдена: {e.text[:60]}"
        if e.kind == "paraphrase":
            assert not any(f["kind"] == "lost" and f["a"] and norm(f["a"][0]["text"]) == norm(e.text) for f in findings), (
                "переформулировка это не потеря"
            )
        if e.kind == "dup":
            assert any(
                norm(e.text) in {norm(c["text"]) for c in g["clauses"]} and {e.dept, e.dept_to} <= {_owner_abbr(c) for c in g["clauses"]}
                for g in r["duplicates"]
            ), f"дубль не найден: {e.text[:60]}"
    expected = {norm(e.text) for e in exp if e.kind in {"lose", "move"}}
    false_losses = [
        f
        for f in findings
        if f["kind"] == "lost" and f["a"] and not f["a"][0].get("lead") and f["a"][0]["section"] == "4" and norm(f["a"][0]["text"]) not in expected
    ]
    assert not false_losses, [f["a"][0]["text"][:60] for f in false_losses]
