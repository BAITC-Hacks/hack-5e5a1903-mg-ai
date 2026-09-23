"""Разбор документа в пункты: идентификаторы, наследование владельца, модальность, склейки."""

from orgdiff.parse import parse_paragraphs, split_inline


def test_inline_numbered_clauses_are_split_by_expected_next_number():
    pieces = split_inline("3.9", "Рабочие места работников. 3.10.Работники могут выполнять функции. 3.11.По вопросам дисциплины.")
    assert [p[0] for p in pieces] == ["3.9", "3.10", "3.11"]
    assert pieces[1][1].startswith("Работники могут")


def test_inline_split_does_not_break_on_references_inside_text():
    # «п. 5.8.1 и 5.8.2» это ссылки, а не начало пунктов 5.11.3
    pieces = split_inline("5.11.2", "доступ к документам в части, не противоречащей п. 5.8.1 и 5.8.2;")
    assert len(pieces) == 1


def test_owner_is_inherited_from_role_heading_and_kept_across_nested_leads():
    paras = [
        "5. Права и обязанности",
        "5.3. Директоры департаментов и Директоры направлений:",
        "5.3.1. обеспечивают стратегическое управление;",
        "5.3.2. организуют по решению Главного аудитора руководство проверок:",
        "а. аудит ИТ систем;",
        "5.3.3. готовят предложения для включения в план работ БВА;",
    ]
    cl = {c.id: c for c in parse_paragraphs(paras, "t")}
    assert cl["5.3.1"].owner == "Директоры департаментов и Директоры направлений"
    assert cl["5.3.2.а"].owner == cl["5.3.1"].owner, "вложенная вводная фраза не меняет владельца"
    assert cl["5.3.3"].owner == cl["5.3.1"].owner, "после вложенного блока владелец восстанавливается"
    assert cl["5.3.1"].modality == "duty"


def test_modality_comes_from_the_last_modal_phrase_in_the_heading():
    paras = [
        "5. Права и обязанности",
        "5.6. Директоры департаментов обязаны обеспечить выполнение задач, а также имеют право:",
        "5.6.1. выносить предложения по внесению изменений в программы;",
        "5.8. Главный аудитор и работники БВА не имеют права:",
        "5.8.1. руководить действиями работников других подразделений;",
    ]
    cl = {c.id: c for c in parse_paragraphs(paras, "t")}
    assert cl["5.6.1"].modality == "right"
    assert cl["5.8.1"].modality == "prohibition"


def test_unnumbered_role_heading_scopes_following_clauses():
    paras = ["5. Права и обязанности", "Главный аудитор:", "5.1. Организует работу БВА:", "5.1.1. организует подготовку плана работ;"]
    cl = {c.id: c for c in parse_paragraphs(paras, "t")}
    assert cl["5.1.1"].owner == "Главный аудитор"


def test_control_document_parses_all_numbered_clauses(clauses_before, clauses_after):
    numbered_a = [c for c in clauses_before if c.num]
    numbered_b = [c for c in clauses_after if c.num]
    assert len(numbered_a) >= 460 and len(numbered_b) >= 460
    ids = [c.id for c in clauses_before]
    assert len(ids) == len(set(ids)), "идентификаторы уникальны"
    assert any(c.id == "5.6.2" for c in clauses_before)


def test_control_document_toc_is_not_parsed_as_sections(clauses_after):
    """В PDF оглавление в конце документа выглядит как заголовки разделов с номером страницы."""
    section_5 = [c for c in clauses_after if c.id == "5"]
    assert len(section_5) == 1
    assert "18" not in section_5[0].text
