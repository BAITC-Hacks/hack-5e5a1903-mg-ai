"""Выравнивание: монотонное ДП со слияниями и разделениями."""

from orgdiff.align import align
from orgdiff.similarity import Sim


def _sim(*texts):
    return Sim(list(texts), mode="tfidf")


def test_identical_sequences_align_one_to_one_without_gaps():
    a = ["организует работу департамента;", "готовит предложения в план работ БВА;", "контролирует устранение недостатков;"]
    al = align(a, list(a), _sim(*a), tau=0.45)
    assert len(al.links) == 3 and not al.gaps_a and not al.gaps_b
    assert all(ln.a == ln.b for ln in al.links)


def test_deleted_clause_becomes_a_gap_and_numbering_shift_does_not_matter():
    a = [
        "организует работу департамента;",
        "готовит предложения в план работ БВА;",
        "контролирует устранение недостатков;",
        "взаимодействует с руководителями Общества;",
    ]
    b = [a[0], a[2], a[3]]
    al = align(a, b, _sim(*a), tau=0.45)
    assert al.gaps_a == [1] and not al.gaps_b
    assert len(al.links) == 3


def test_two_clauses_merged_into_one_are_linked_as_two_to_one():
    a = [
        "ведет переписку с Руководителями Общества по вопросам зоны ответственности;",
        "присутствует на заседаниях органов управления в качестве наблюдателя;",
    ]
    b = [
        "ведет переписку с Руководителями Общества по вопросам зоны ответственности и присутствует на заседаниях органов управления в качестве наблюдателя;"
    ]
    al = align(a, b, _sim(*a, *b), tau=0.45)
    assert not al.gaps_a and not al.gaps_b
    assert len(al.links) == 1 and al.links[0].a == [0, 1] and al.links[0].b == [0]


def test_control_documents_self_alignment_has_no_gaps(clauses_before):
    texts = [c.text for c in clauses_before]
    al = align(texts, texts, Sim(texts, mode="tfidf"), tau=0.45)
    assert not al.gaps_a and not al.gaps_b
    assert len(al.links) == len(texts)
