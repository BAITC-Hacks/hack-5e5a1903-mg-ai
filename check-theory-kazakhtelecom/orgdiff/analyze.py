"""Превращение выравнивания в выводы: структура, потери, переносы, слияния, дубли, конфликт интересов."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .align import Alignment
from .parse import Clause, departments
from .similarity import Sim

COI_PATTERN = re.compile(r"конфликт[а-я]*\s+интерес|\bКИ\b|совмещ", re.I)


@dataclass
class Finding:
    kind: str
    a_ids: list[str] = field(default_factory=list)
    b_ids: list[str] = field(default_factory=list)
    sim: float = 0.0
    note: str = ""
    llm: dict | None = None
    candidate: str | None = None  # для потери: ближайший по тексту пункт новой редакции, который функцию не покрывает


def structure_changes(A: list[Clause], B: list[Clause]) -> dict:
    """Обертка для старых вызовов: структура из пунктов. Структура из Excel идет
    через proto.structure напрямую."""
    from .structure import from_clauses
    from .structure import structure_changes as _sc

    return _sc(from_clauses(A), from_clauses(B))


def owner_roles(owner: str | None, depts: dict[str, str]) -> frozenset[str]:
    """Строка владельца -> множество канонических ролей.

    Владелец в документе это фраза, и одна фраза может означать несколько ролей:
    "Директоры департаментов" это все четыре директора. Без множеств нельзя отличить
    "функция ушла в общий блок, где владелец тоже есть" от "функция снята с владельца".
    """
    if not owner:
        return frozenset()
    low = owner.lower()
    roles: set[str] = set()
    for abbr, full in depts.items():
        # аббревиатура только целым словом: «ДАД» не должна совпадать внутри «ДАДО»
        if re.search(r"(?<![а-яёa-z])" + re.escape(abbr.lower()) + r"(?![а-яёa-z])", low) or full.lower() in low:
            roles.add(abbr)
    if "директоры департаментов" in low:
        roles |= set(depts)
    if "направлени" in low and "директор" in low:
        roles.add("НАПРАВЛЕНИЕ")
    if "главн" in low and "аудитор" in low:
        roles.add("ГА")
    if "работники бва" in low:
        roles.add("БВА")
    return frozenset(roles) if roles else frozenset({owner[:50]})


class Classifier:
    """Держит контекст выравнивания, чтобы одну и ту же логику разрыва применять
    и в первом проходе, и после того, как модель разорвала сомнительную пару."""

    def __init__(self, al: Alignment, A: list[Clause], B: list[Clause], sim: Sim, tau_global: float, high: float, merge_tau: float = 0.75):
        self.A, self.B, self.sim = A, B, sim
        self.tau_global, self.high = tau_global, high
        # порог, с которого кандидат из блока того же владельца считается слиянием.
        # Ниже high, потому что общий пункт часто длиннее ("готовят предложения ...,
        # взаимодействуют ..."), и близость к нему падает. Ниже high слияние потом
        # подтверждается моделью, если она есть.
        self.merge_tau = merge_tau
        self.depts_a, self.depts_b = departments(A), departments(B)
        self.linked_b = {j for ln in al.links for j in ln.b}
        self.linked_a = {i for ln in al.links for i in ln.a}
        self.texts_a = [c.text for c in A]
        self.texts_b = [c.text for c in B]
        self.al = al

    def gap_a(self, i: int, allow_merge: bool = True) -> Finding:
        """Пункт «до» без пары. Смотрим всех кандидатов выше порога, а не одного лучшего:
        если хоть один живет в блоке, где владелец пункта есть, функция у него осталась."""
        a = self.A[i]
        row = self.sim.matrix([a.text], self.texts_b)[0]
        order = np.argsort(-row)
        best_j, best = int(order[0]), float(row[order[0]])
        if allow_merge and best >= self.merge_tau:
            ra = owner_roles(a.owner, self.depts_a)
            for j in order[:5]:
                if row[j] < self.merge_tau:
                    break
                rb = owner_roles(self.B[j].owner, self.depts_b)
                if ra & rb:
                    note = f"{a.owner} -> {self.B[j].owner}: владелец в новом блоке есть"
                    if row[j] < self.high:
                        note += " | ниже high, нужно подтверждение"
                    return Finding("merged", [a.id], [self.B[j].id], float(row[j]), note)
        if best >= self.high:
            if best_j in self.linked_b:
                return Finding(
                    "removed_from_owner",
                    [a.id],
                    [self.B[best_j].id],
                    best,
                    f"снята с «{a.owner}», остается у «{self.B[best_j].owner}» ({self.B[best_j].id})",
                )
            return Finding("moved_to", [a.id], [self.B[best_j].id], best, f"{a.owner} -> {self.B[best_j].owner}")
        if best >= self.tau_global:
            return Finding("partial", [a.id], [self.B[best_j].id], best, f"{a.owner} -> {self.B[best_j].owner}")
        return Finding("lost", [a.id], [], best, f"лучший кандидат {self.B[best_j].id} ({best:.2f})", candidate=self.B[best_j].id)

    def gap_b(self, j: int) -> Finding:
        b = self.B[j]
        row = self.sim.matrix([b.text], self.texts_a)[0]
        i = int(np.argmax(row))
        best = float(row[i])
        if best >= self.high:
            ra, rb = owner_roles(self.A[i].owner, self.depts_a), owner_roles(b.owner, self.depts_b)
            if i in self.linked_a and not (ra & rb):
                return Finding(
                    "added_to_owner", [self.A[i].id], [b.id], best, f"функция «{self.A[i].owner}» ({self.A[i].id}) добавлена владельцу «{b.owner}»"
                )
            return Finding("moved_from", [self.A[i].id], [b.id], best, f"{self.A[i].owner} -> {b.owner}")
        if best >= self.tau_global:
            return Finding("partial_new", [self.A[i].id], [b.id], best, f"{self.A[i].owner} -> {b.owner}")
        return Finding("new", [], [b.id], best, f"лучший кандидат {self.A[i].id} ({best:.2f})")

    def links(self) -> list[Finding]:
        from .verify import _stems

        out = []
        for ln in self.al.links:
            a_cl, b_cl = [self.A[i] for i in ln.a], [self.B[j] for j in ln.b]
            kind = "unchanged" if ln.sim >= self.high else "modified"
            owners_a = {c.owner for c in a_cl if c.owner}
            owners_b = {c.owner for c in b_cl if c.owner}
            note = ""
            if kind == "unchanged" and ln.sim < 0.995:
                # близость высокая, но тексты не идентичны. Короткий отрезанный хвост вроде
                # «Главному аудитору» порог не сбивает, поэтому смотрим на основы слов:
                # пропало два и больше значимых слов -> пара считается изменённой и уходит на проверку
                sa = set(_stems(" ".join(c.text for c in a_cl)))
                sb = set(_stems(" ".join(c.text for c in b_cl)))
                missing = sorted(s for s in sa - sb if len(s) >= 5)
                if len(missing) >= 1:
                    kind = "modified"
                    note = "в новом пункте нет слов: " + ", ".join(missing[:6])
            if owners_a and owners_b and owners_a != owners_b:
                kind = "reassigned"
                note = f"{' / '.join(owners_a)} -> {' / '.join(owners_b)}"
            if len(ln.a) > 1:
                note = (note + " | " if note else "") + "слияние 2:1"
            if len(ln.b) > 1:
                note = (note + " | " if note else "") + "разделение 1:2"
            out.append(Finding(kind, [c.id for c in a_cl], [c.id for c in b_cl], ln.sim, note))
        return out


def classify(al: Alignment, A: list[Clause], B: list[Clause], sim: Sim, tau_global: float = 0.6, high: float = 0.85, merge_tau: float = 0.75):
    cl = Classifier(al, A, B, sim, tau_global, high, merge_tau=merge_tau)
    out = cl.links()
    out += [cl.gap_a(i) for i in al.gaps_a]
    out += [cl.gap_b(j) for j in al.gaps_b]
    return out, cl


def apply_verification(
    findings: list[Finding],
    cl: Classifier,
    verifier,
    top_k: int = 5,
    function_sections: frozenset[str] = frozenset({"4", "5"}),
):
    """Вердикт модели там, где близость не дает уверенности.

    - partial: спрашиваем покрытие. full -> covered, partial -> partial_loss, none -> lost.
    - lost: показываем top-k кандидатов. Защита от ложной потери.
    - modified / reassigned с близостью ниже high: спрашиваем покрытие. partial -> partial_loss,
      none -> пара разрывается, обе стороны заново классифицируются как разрывы.
    Вердикт с неподтвержденной цитатой класс вывода не меняет.
    """
    A, B = cl.A, cl.B
    by_a = {c.id: c for c in A}
    by_b = {c.id: c for c in B}
    idx_a = {c.id: i for i, c in enumerate(A)}
    idx_b = {c.id: j for j, c in enumerate(B)}

    def is_function(c: Clause) -> bool:
        return c.section in function_sections and c.is_leaf and bool(c.owner)

    extra: list[Finding] = []
    for f in findings:
        if f.kind == "merged" and f.sim < cl.high:
            a, b = by_a[f.a_ids[0]], by_b[f.b_ids[0]]
            d = verifier.coverage_vote(a.text, b.text)
            f.llm = d
            if not d["quotes_ok"]:
                f.note += " | цитата не подтверждена, класс не менялся"
                continue
            if d["coverage"] == "full" or (d["coverage"] == "partial" and not d["functional"]):
                f.note += f" | LLM подтвердила слияние {d['votes']}"
            else:
                # слияние не подтверждено: кандидат из общего блока про другое или покрывает
                # не всё. Слабый кандидат не должен превращать полную потерю в частичную,
                # поэтому пункт заново классифицируется как обычный разрыв, без слияния.
                g = cl.gap_a(idx_a[a.id], allow_merge=False)
                f.kind, f.b_ids, f.sim = g.kind, g.b_ids, g.sim
                f.note = g.note + f" | слияние с {b.id} не подтверждено {d['votes']}"
                if g.kind == "partial":
                    # лучший кандидат тот же, вердикт по нему уже есть
                    if d["coverage"] == "partial" and d["functional"]:
                        f.kind = "partial_loss"
                        f.note += f" | LLM: частично, пропало [{d['lost_kind']}]: {d['lost_part']}"
                    elif d["coverage"] == "none":
                        f.kind = "lost"
                        f.note += " | LLM: кандидат про другое"
        elif f.kind == "partial":
            a, b = by_a[f.a_ids[0]], by_b[f.b_ids[0]]
            if not is_function(a):
                continue
            d = verifier.coverage_vote(a.text, b.text)
            f.llm = d
            if not d["quotes_ok"]:
                f.note += " | цитата не подтверждена, класс не менялся"
                continue
            if d["coverage"] == "partial" and not d["functional"]:
                f.kind = "covered"
                f.note += f" | LLM: пропала только формулировка ({d['lost_kind']}: {d['lost_part'][:60]})"
                continue
            f.kind = {"full": "covered", "partial": "partial_loss", "none": "lost"}[d["coverage"]]
            if f.kind == "lost":
                f.candidate, f.b_ids = f.b_ids[0], []
            f.note += f" | LLM: {d['coverage']} {d.get('votes', '')}" + (
                f", пропало [{d['lost_kind']}]: {d['lost_part']}" if d["coverage"] == "partial" else ""
            )
        elif f.kind == "lost":
            a = by_a[f.a_ids[0]]
            row = cl.sim.matrix([a.text], cl.texts_b)[0]
            idx = np.argsort(-row)[:top_k]
            cands = [(B[j].id, B[j].text) for j in idx]
            d = verifier.find(a.text, cands)
            f.llm = d
            if not d["quotes_ok"]:
                f.note += " | цитата не подтверждена, класс не менялся"
                continue
            if d["found"] and d["coverage"] == "full":
                f.kind, f.b_ids = "covered", [d["candidate_id"]]
                f.note += f" | LLM нашла в {d['candidate_id']}"
            elif d["found"] and d["coverage"] == "partial":
                f.note += f" | LLM: частично похоже на {d['candidate_id']}, потеря не снята"
            else:
                f.note += " | LLM подтвердила потерю"
        elif f.kind in {"modified", "reassigned"} and (f.sim < cl.high or "нет слов" in f.note):
            a_cl = [by_a[x] for x in f.a_ids]
            b_cl = [by_b[y] for y in f.b_ids]
            if not any(is_function(c) for c in a_cl + b_cl):
                continue
            if not any(is_function(c) for c in a_cl) and any(is_function(c) for c in b_cl):
                # заголовок блока склеился с функцией: заголовок ничего не покрывает,
                # рвем без вопроса к модели, иначе она ответит "частично" и породит ложную потерю
                f.kind = "unlinked"
                f.note += " | заголовок против функции, пара разорвана"
                for c in a_cl:
                    cl.linked_a.discard(idx_a[c.id])
                for c in b_cl:
                    cl.linked_b.discard(idx_b[c.id])
                for c in b_cl:
                    g = cl.gap_b(idx_b[c.id])
                    g.note += " | после разрыва пары"
                    extra.append(g)
                continue
            # для слияний 2:1 и разделений 1:2 сравниваем склейки, цитаты ищутся в склейке
            a_text = " ".join(c.text for c in a_cl)
            b_text = " ".join(c.text for c in b_cl)
            d = verifier.coverage_vote(a_text, b_text)
            f.llm = d
            if not d["quotes_ok"]:
                f.note += " | цитата не подтверждена"
                continue
            if d["coverage"] == "partial" and d["functional"]:
                f.kind = "partial_loss"
                f.note += f" | LLM: частично {d['votes']}, пропало [{d['lost_kind']}]: {d['lost_part']}"
            elif d["coverage"] == "partial":
                f.note += f" | LLM: только формулировка ({d['lost_kind']})"
            elif d["coverage"] == "none":
                # выравнивание склеило разные пункты. Разрываем и смотрим на каждую сторону как на разрыв.
                f.kind = "unlinked"
                f.note += f" | LLM: пункты про разное {d['votes']}, пара разорвана"
                for c in a_cl:
                    cl.linked_a.discard(idx_a[c.id])
                for c in b_cl:
                    cl.linked_b.discard(idx_b[c.id])
                for c in a_cl:
                    g = cl.gap_a(idx_a[c.id])
                    g.note += " | после разрыва пары"
                    extra.append(g)
                for c in b_cl:
                    g = cl.gap_b(idx_b[c.id])
                    g.note += " | после разрыва пары"
                    extra.append(g)
    findings += extra
    return findings


def duplicates(B: list[Clause], sim: Sim, tau_dup: float = 0.6, sections: set[str] = frozenset({"4", "5"})) -> list[list[Clause]]:
    """Кластеры похожих функций у разных владельцев внутри документа «после»."""
    funcs = [c for c in B if c.is_leaf and c.owner and c.modality in {"duty", "right"} and c.num and c.section in sections]
    if not funcs:
        return []
    S = sim.matrix([c.text for c in funcs], [c.text for c in funcs], combine="max")
    parent = list(range(len(funcs)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(funcs)):
        for j in range(i + 1, len(funcs)):
            if S[i, j] >= tau_dup and funcs[i].owner != funcs[j].owner:
                parent[find(i)] = find(j)
    groups: dict[int, list[Clause]] = {}
    for i, c in enumerate(funcs):
        groups.setdefault(find(i), []).append(c)
    return [g for g in groups.values() if len({c.owner for c in g}) >= 2]


def verify_duplicates(dups: list[list[Clause]], verifier) -> list[tuple[list[Clause], dict]]:
    return [(g, verifier.same_function([(c.id, c.owner or "", c.text) for c in g])) for g in dups]


def conflicts_of_interest(B: list[Clause]) -> list[Clause]:
    return [c for c in B if COI_PATTERN.search(c.text)]


def verify_coi(coi: list[Clause], verifier) -> dict[str, dict]:
    """id пункта -> вердикт модели по конфликту интересов. Пачка до 20 пунктов на вызов."""
    out: dict[str, dict] = {}
    for i in range(0, len(coi), 20):
        batch = coi[i : i + 20]
        for v in verifier.coi_assess([(c.id, c.owner or "", c.text) for c in batch]):
            out[v["id"]] = v
    return out
