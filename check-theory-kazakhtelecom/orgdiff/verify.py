"""Вердикт модели по спорным местам. Три вопроса, каждый со строгой JSON-схемой:

1. coverage: покрывает ли новый пункт старый целиком, частично или никак. Задается
   парам с низкой близостью и всем "частично покрытым".
2. find: есть ли функция старого пункта среди нескольких кандидатов. Задается
   потерям как защита от ложной потери.
3. same_function: описывают ли пункты кластера одну функцию. Задается кластерам дублей.

Во всех трех модель обязана вернуть дословные цитаты. Цитата проверяется как подстрока
исходного пункта. Вердикт с выдуманной цитатой помечается и не считается подтвержденным.
Это та самая механическая защита от галлюцинаций из плана.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SYSTEM = (
    "Ты аналитик организационных документов. Отвечай строго по тексту переданных пунктов, "
    "ничего не домысливай. Все цитаты приводи дословно, без изменений и сокращений, "
    "как непрерывный фрагмент исходного пункта. Отвечай по-русски."
)

# USD за миллион токенов, вход / выход
PRICES = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5.1": (1.25, 10.00),
}


def _norm(s: str) -> str:
    s = re.sub(r"^\s*\[[^\]]{1,20}\]\s*", "", s)  # модель иногда копирует маркер [5.5] из списка кандидатов
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".;:,")


def quote_ok(quote: str, source: str) -> bool:
    q = _norm(quote)
    return bool(q) and q in _norm(source)


# виды пропавшей части: все, кроме wording, считаются потерей функции.
# scope тоже функциональна: модель называет так сужение круга лиц или области
# ("работников БВА" -> "работников департамента"), и это реальное сужение права.
LOST_KINDS = ["action", "right", "object", "period", "persons", "scope", "wording"]
FUNCTIONAL_LOSS = {"action", "right", "object", "period", "persons", "scope"}

# признаки, по которым фраза с меткой wording все же считается действием:
# отглагольное существительное в начале и не предлог-уточнение
VERBAL_NOUN = re.compile(
    r"^(организац|формирован|проведен|подготовк|разработк|анализ|контрол|оценк|учет|участ|представлен|согласован|рассмотрен|утвержден|информирован|мониторинг|планирован)"
)
QUALIFIER_START = re.compile(r"^(в соответствии|согласно|в рамках|в порядке|в целях|с учетом|при |в части|для |по |на |с |о |об )")


def _stems(s: str) -> list[str]:
    """Грубые основы для русского: первые 5 букв слов длиннее 3. Хватает, чтобы
    "анализирует" и "анализируют" или "работников" и "работниках" совпали."""
    # слова от трех букв: аббревиатуры вроде БВА и ДНМ это содержательные слова,
    # именно их исчезновение и есть сужение "работников БВА" до "работников департамента"
    return [w[:5] for w in re.findall(r"[а-яё]+", s.lower()) if len(w) >= 3]


def stems_present(part: str, source: str, ratio: float = 0.8) -> bool:
    ps = _stems(part)
    if not ps:
        return False
    src = set(_stems(source))
    return sum(1 for s in ps if s in src) / len(ps) >= ratio


@dataclass
class Verifier:
    model: str = "gpt-4.1-mini"
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    bad_quotes: int = 0
    log: list[dict] = field(default_factory=list)
    on_call: object = None  # колбэк прогресса: вызывается после каждого обращения с номером обращения

    def __post_init__(self):
        from .llm import make_client

        self.client = make_client()

    @property
    def cost_usd(self) -> float:
        pin, pout = PRICES.get(self.model, PRICES["gpt-4.1-mini"])
        return (self.tokens_in * pin + self.tokens_out * pout) / 1e6

    def _ask(self, name: str, schema: dict, user: str) -> dict:
        kwargs = {} if self.model.startswith(("gpt-5", "o")) else {"temperature": 0}  # gpt-5 не принимает temperature
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            **kwargs,
        )
        self.calls += 1
        self.tokens_in += resp.usage.prompt_tokens
        self.tokens_out += resp.usage.completion_tokens
        data = json.loads(resp.choices[0].message.content)
        self.log.append({"q": name, "answer": data})
        if self.on_call:
            self.on_call(self.calls)
        return data

    # 1 ------------------------------------------------------------------
    def coverage(self, a_text: str, b_text: str) -> dict:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "coverage": {"type": "string", "enum": ["full", "partial", "none"]},
                "lost_part": {"type": "string", "description": "дословная цитата пропавшей части старого пункта, пусто если full или none"},
                "lost_kind": {
                    "type": "string",
                    "enum": LOST_KINDS,
                    "description": "что именно пропало: action действие, right право, object объект действия, period срок или периодичность, persons адресат или круг лиц, scope сужение области или круга (было «все работники БВА», стало «работники департамента»), wording только формулировка, включая число и падеж",
                },
                "quote_old": {"type": "string", "description": "дословная цитата из старого пункта, подтверждающая вывод"},
                "quote_new": {"type": "string", "description": "дословная цитата из нового пункта, пусто если none"},
                "reason": {"type": "string"},
            },
            "required": ["coverage", "lost_part", "lost_kind", "quote_old", "quote_new", "reason"],
        }
        user = (
            "Сравни функцию из старой редакции с пунктом новой редакции.\n"
            "full: новый пункт покрывает старую функцию целиком. Другой порядок слов, синонимы, "
            "сокращения, множественное число, убранные ссылки вроде «в соответствии с Положением», "
            "а также ДОБАВЛЕНИЯ в новом пункте это все еще full: добавленное не является потерей.\n"
            "partial: в старом пункте есть конкретное действие, право, объект, срок, периодичность "
            "или круг лиц, которого в новом пункте нет. Типичный случай: старый пункт перечисляет "
            "два действия через «и» или точку с запятой, а новый оставил одно, тогда пропавшее "
            "действие и есть lost_part с lost_kind = action. В lost_part приведи только пропавшую "
            "часть дословной цитатой из старого пункта, не весь пункт, а в lost_kind укажи, что это "
            "за часть. Если пропало только слово-уточнение без смены сути, ставь lost_kind = wording.\n"
            "none: новый пункт про другое.\n"
            "Цитаты приводи без каких-либо маркеров и номеров пунктов.\n\n"
            f"СТАРЫЙ ПУНКТ:\n{a_text}\n\nНОВЫЙ ПУНКТ:\n{b_text}"
        )
        d = self._ask("coverage", schema, user)
        quotes = quote_ok(d["quote_old"], a_text) and (d["coverage"] == "none" or quote_ok(d["quote_new"], b_text))
        # пропавшая часть обязана быть цитатой старого пункта: если модель ее пересказала
        # или выдумала, вердикт partial не засчитывается
        if d["coverage"] == "partial" and not quote_ok(d["lost_part"], a_text):
            quotes = False
        # если "пропавшая часть" это весь старый пункт целиком, ничего конкретного не пропало:
        # модель так отвечает на смену числа глагола. Это формулировка, а не потеря.
        if d["coverage"] == "partial" and len(_norm(d["lost_part"])) >= 0.8 * len(_norm(a_text)):
            d["lost_kind"] = "wording"
            d["reason"] = "пропавшая часть почти равна всему пункту, конкретная потеря не выделена; " + d["reason"]
        # если "пропавшая часть" есть и в новом пункте, пусть в другом числе или падеже,
        # она не пропала. Так модель отвечает на надмножество и на смену числа глагола.
        if d["coverage"] == "partial" and (quote_ok(d["lost_part"], b_text) or stems_present(d["lost_part"], b_text)):
            d["coverage"] = "full"
            d["reason"] = "пропавшая часть присутствует в новом пункте; " + d["reason"]
        d["quotes_ok"] = quotes
        # метка lost_kind у модели совещательная: она ставит wording на "организация работы
        # проектной команды", правильно описывая потерю в reason. Решает механика:
        # пропавшая часть есть в старом, отсутствует в новом, короче 80% старого;
        # метка wording перебивается, только если фраза начинается с отглагольного
        # существительного и не является предлогом-уточнением вроде "в соответствии с ...".
        lp = _norm(d["lost_part"])
        mechanical = d["coverage"] == "partial" and bool(lp) and lp in _norm(a_text) and len(lp) < 0.8 * len(_norm(a_text))
        looks_like_action = bool(VERBAL_NOUN.match(lp)) and not QUALIFIER_START.match(lp)
        d["functional"] = mechanical and (d["lost_kind"] in FUNCTIONAL_LOSS or looks_like_action)
        if not d["quotes_ok"]:
            self.bad_quotes += 1
        return d

    def coverage_vote(self, a_text: str, b_text: str, n: int = 3) -> dict:
        """Голосование n вызовов. Модель недетерминирована даже при нулевой температуре
        и склонна называть потерей любое пропавшее слово; одиночный вердикт дрожит между
        прогонами. Берется большинство по coverage, а functional требует большинства
        среди вердиктов partial. Цитаты берутся из победившего вердикта."""
        votes = [self.coverage(a_text, b_text) for _ in range(n)]
        cov = max({"full", "partial", "none"}, key=lambda c: sum(v["coverage"] == c for v in votes))
        chosen = [v for v in votes if v["coverage"] == cov]
        if cov == "partial":
            functional_votes = sum(v["functional"] for v in chosen)
            chosen.sort(key=lambda v: (not v["quotes_ok"], not v["functional"]))
            d = dict(chosen[0])
            d["functional"] = functional_votes * 2 > n  # большинство из n, а не из chosen
        else:
            chosen.sort(key=lambda v: not v["quotes_ok"])
            d = dict(chosen[0])
        d["votes"] = [v["coverage"] + ("+" if v.get("functional") else "") for v in votes]
        return d

    # 2 ------------------------------------------------------------------
    def find(self, a_text: str, candidates: list[tuple[str, str]]) -> dict:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "found": {"type": "boolean"},
                "candidate_id": {"type": "string", "description": "идентификатор кандидата или пустая строка"},
                "coverage": {"type": "string", "enum": ["full", "partial", "none"]},
                "quote_candidate": {"type": "string", "description": "дословная цитата из выбранного кандидата, пусто если none"},
                "reason": {"type": "string"},
            },
            "required": ["found", "candidate_id", "coverage", "quote_candidate", "reason"],
        }
        cands = "\n\n".join(f"[{cid}] {txt}" for cid, txt in candidates)
        user = (
            "Функция из старой редакции ниже не нашла пары при выравнивании. Есть ли она среди кандидатов "
            "из новой редакции? Выбери один кандидат, если функция там присутствует полностью или частично. "
            "Похожие слова при другой функции это none. Цитату приводи без маркера в квадратных скобках.\n\n"
            f"СТАРЫЙ ПУНКТ:\n{a_text}\n\nКАНДИДАТЫ:\n{cands}"
        )
        d = self._ask("find", schema, user)
        src = dict(candidates).get(d["candidate_id"], "")
        d["quotes_ok"] = (not d["found"]) or quote_ok(d["quote_candidate"], src)
        if not d["quotes_ok"]:
            self.bad_quotes += 1
        return d

    # 3 ------------------------------------------------------------------
    def coi_assess(self, items: list[tuple[str, str, str]]) -> list[dict]:
        """Пункты, где упомянут конфликт интересов, найдены по словам. Модель делит их:
        risk, если пункт создает или допускает конфликт (совмещение функций, зависимость
        от проверяемого), safeguard, если это защитная норма (запрет, декларация, отвод),
        mention, если это просто упоминание. Цитата обязана быть в тексте пункта, иначе
        вердикт понижается до mention."""
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "verdict": {"type": "string", "enum": ["risk", "safeguard", "mention"]},
                            "quote": {"type": "string", "description": "дословная цитата из пункта, на которой основан вердикт"},
                            "reason": {"type": "string"},
                        },
                        "required": ["id", "verdict", "quote", "reason"],
                    },
                }
            },
            "required": ["items"],
        }
        block = "\n\n".join(f"[{cid}] владелец: {owner}\n{txt}" for cid, owner, txt in items)
        user = (
            "Ниже пункты положения, в которых упомянут конфликт интересов или совмещение функций. "
            "Для каждого определи: risk, если пункт закрепляет положение, при котором конфликт интересов "
            "возникает или допускается (одна роль и выполняет, и проверяет; аудитор зависит от проверяемого; "
            "совмещение несовместимых функций); safeguard, если пункт защищает от конфликта "
            "(запрет, обязанность сообщить, отвод, независимость); mention, если это просто упоминание "
            "термина без нормы. Цитируй дословно.\n\n" + block
        )
        d = self._ask("coi_assess", schema, user)
        src = {cid: txt for cid, _, txt in items}
        out = []
        for it in d["items"]:
            ok = quote_ok(it["quote"], src.get(it["id"], ""))
            if not ok:
                self.bad_quotes += 1
            out.append({**it, "quotes_ok": ok, "verdict": it["verdict"] if ok else "mention"})
        return out

    def same_function(self, items: list[tuple[str, str, str]]) -> dict:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": ["same", "overlap", "different"]},
                "shared_function": {"type": "string", "description": "общая функция одной фразой, пусто если different"},
                "quotes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"id": {"type": "string"}, "quote": {"type": "string"}},
                        "required": ["id", "quote"],
                    },
                },
                "reason": {"type": "string"},
            },
            "required": ["verdict", "shared_function", "quotes", "reason"],
        }
        block = "\n\n".join(f"[{cid}] владелец: {owner}\n{txt}" for cid, owner, txt in items)
        user = (
            "Пункты ниже принадлежат разным ролям одного документа. Описывают ли они одну и ту же функцию?\n"
            "same: одна функция у нескольких ролей. overlap: функции пересекаются частично. "
            "different: функции разные, сходство только в словах.\nДля same и overlap приведи по цитате из каждого пункта.\n\n" + block
        )
        d = self._ask("same_function", schema, user)
        src = {cid: txt for cid, _, txt in items}
        d["quotes_ok"] = d["verdict"] == "different" or all(quote_ok(q["quote"], src.get(q["id"], "")) for q in d["quotes"])
        if not d["quotes_ok"]:
            self.bad_quotes += 1
        return d
