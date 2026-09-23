"""Контрольный комплект организаторов: известные изменения обязаны находиться.

Это проверка из раздела 11 ТЗ: реорганизация подразделения, потеря функции,
дублирование функционала, корректные источники. Без модели детерминированно."""

import os

import pytest

from orgdiff.golden import GROUND_TRUTH


def _checks(result, tier):
    return [c for c in result["golden"]["checks"] if c["tier"] == tier]


def test_reorganisation_is_detected(control_result):
    s = control_result["structure"]
    assert s["departments_created"] == ["ДИТААД", "ДОА"]
    assert s["departments_kept"] == ["ДККМ", "ДНМ"]
    assert "Директор направления внутреннего аудита" in s["positions_removed"]


def test_lost_functions_are_detected_with_sources(control_result):
    lost = {f["a"][0]["id"]: f for f in control_result["findings"] if f["kind"] == "lost" and f["a"]}
    for clause_id in ("5.6.2", "5.6.3", "5.7.2"):
        assert clause_id in lost, f"потеря {clause_id} не найдена"
        f = lost[clause_id]
        assert f["a"][0]["doc"] and f["a"][0]["text"], "у вывода есть документ и текст пункта"
        assert f["candidate"] is not None, "показан ближайший пункт новой редакции"


def test_merged_rights_are_not_reported_as_losses(control_result):
    lost_ids = {f["a"][0]["id"] for f in control_result["findings"] if f["kind"] == "lost" and f["a"]}
    for clause_id in ("5.7.3", "5.7.4", "5.7.5"):
        assert clause_id not in lost_ids, f"{clause_id} влилось в общий блок, это не потеря"


def test_function_transfer_between_sections_is_detected(control_result):
    moved = [f for f in control_result["findings"] if f["kind"] in {"moved_to", "moved_from", "merged", "reassigned"}]
    assert any(any(x["id"].startswith("5.4.4") for x in f["a"]) and any(y["id"].startswith("5.3.3") for y in f["b"]) for f in moved)


def test_duplicates_are_detected_with_both_sources(control_result):
    clusters = [{c["id"] for c in g["clauses"]} for g in control_result["duplicates"]]
    for x, y in (("5.3.6", "5.4.3"), ("5.3.12", "5.4.9"), ("5.3.8", "5.4.5")):
        assert any(x in ids and y in ids for ids in clusters), f"дубль {x} ~ {y} не найден"
    for g in control_result["duplicates"]:
        assert all(c["doc"] and c["text"] for c in g["clauses"])


def test_conflict_of_interest_clause_is_found(control_result):
    assert any(c["id"].startswith("4.4") for c in control_result["coi"])


def test_lexical_tier_of_reference_passes_fully(control_result):
    lex = _checks(control_result, "lexical")
    failed = [c["name"] for c in lex if not c["ok"]]
    assert not failed, failed


def test_recall_and_precision_thresholds_without_model(control_result):
    g = control_result["golden"]
    assert g["total"] == len(GROUND_TRUTH)
    assert g["recall"] >= 0.85
    assert g["precision_loss"] >= 0.7


@pytest.mark.llm
@pytest.mark.slow
def test_full_pipeline_with_model_meets_submission_bar(before_pdf, before_xlsx, after_pdf, after_xlsx):
    """Сдаваемая планка: с моделью полнота не ниже 94%, точность по потерям не ниже 85%."""
    from orgdiff.pipeline import run_analysis

    r = run_analysis(
        [before_pdf, before_xlsx],
        [after_pdf, after_xlsx],
        mode="hybrid",
        verify=True,
        golden=True,
        model=os.environ.get("ORGDIFF_MODEL", "gpt-4.1-mini"),
    )
    g = r["golden"]
    assert g["recall"] >= 0.94, g["missed"]
    assert g["precision_loss"] >= 0.85, g["loss_fp"]
    assert r["meta"]["bad_quotes_blocked"] >= 0
    changed = [f for f in r["findings"] if f.get("llm") and f["kind"] in {"partial_loss", "covered"}]
    assert all(f["llm"].get("quotes_ok") for f in changed), "вердикт с неподтверждённой цитатой не должен менять класс"
