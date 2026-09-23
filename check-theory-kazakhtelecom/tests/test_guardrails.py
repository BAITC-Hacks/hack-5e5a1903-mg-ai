"""Механические заслоны против выдумок модели. Именно они делают выводы проверяемыми:
цитата обязана быть в тексте, «пропавшая часть» обязана быть в старом и отсутствовать в новом."""

from orgdiff.verify import QUALIFIER_START, VERBAL_NOUN, _stems, quote_ok, stems_present

OLD = "организация работы проектной команды по проверке и организация контроля качества работы проектной команды;"
NEW = "контроль качества работы проектной команды;"


def test_quote_must_be_substring_of_source():
    assert quote_ok("организация работы проектной команды", OLD)
    assert not quote_ok("организация работы аудиторской группы", OLD)


def test_quote_ignores_case_whitespace_and_trailing_punctuation():
    assert quote_ok("  Организация   работы проектной команды по проверке ;", OLD)


def test_quote_strips_candidate_marker_copied_by_model():
    assert quote_ok("[5.5] контроль качества работы проектной команды", NEW)


def test_empty_quote_is_never_ok():
    assert not quote_ok("", OLD)
    assert not quote_ok("   ", OLD)


def test_stems_tolerate_inflection_but_not_new_words():
    # «анализирует» и «анализируют» одна основа; «БВА» считается содержательным словом
    assert stems_present("анализирует результаты непрерывного аудита", "анализируют результаты непрерывного аудита и готовят материалы")
    assert not stems_present("работников БВА", "работников департаментов в зоне деятельности")


def test_short_abbreviations_count_as_stems():
    assert "бва" in _stems("работников БВА")


def test_verbal_noun_and_qualifier_detection():
    assert VERBAL_NOUN.match("организация работы проектной команды")
    assert not VERBAL_NOUN.match("в соответствии с утвержденным планом")
    assert QUALIFIER_START.match("в соответствии с утвержденным планом работы")
    assert not QUALIFIER_START.match("организация работы проектной команды")
