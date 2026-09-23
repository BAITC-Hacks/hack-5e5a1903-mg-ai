"""Владелец и модальность блока: извлечение моделью по схеме вместо эвристики.

Эвристика в parse.py угадывает роль по первому слову вводной фразы и по глубине пункта.
На контрольном положении это работает, на чужом документе может не сработать. Здесь
каждая вводная фраза («5.3. Директоры департаментов ...:», «Главный аудитор:») отдаётся
модели пачкой, и та по строгой схеме отвечает: вводит ли фраза перечень функций роли,
кто владелец коротким каноническим именем, и какая модальность у перечня.

Ответы кешируются на диске по хешу текста, поэтому повторный разбор того же документа
модель не зовёт. Без ключа резолвер не создаётся, и разбор идёт эвристикой.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

CACHE = Path(__file__).parent.parent / ".cache" / "leads.json"

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "index": {"type": "integer"},
                    "is_role_heading": {
                        "type": "boolean",
                        "description": "фраза вводит перечень функций, прав или запретов конкретной роли или подразделения",
                    },
                    "owner": {
                        "type": "string",
                        "description": "короткое каноническое имя роли: «Директор ДККМ», «Главный аудитор», «Работники БВА», «Директоры департаментов»; пусто, если не роль",
                    },
                    "modality": {
                        "type": "string",
                        "enum": ["duty", "right", "prohibition", "none"],
                        "description": "что перечисляется ниже: обязанности, права, запреты, либо это не перечень функций",
                    },
                },
                "required": ["index", "is_role_heading", "owner", "modality"],
            },
        }
    },
    "required": ["items"],
}

SYSTEM = (
    "Ты аналитик организационных документов. Тебе дают вводные фразы блоков из положения "
    "о подразделении. Отвечай только по тексту фраз, по-русски, строго по схеме."
)


def _key(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


class LeadResolver:
    def __init__(self, model: str = "gpt-4.1-mini", cache: Path = CACHE):
        from .llm import make_client

        self.client = make_client()
        self.model = model
        self.cache_path = cache
        self.calls = 0
        self._cache: dict[str, dict] = {}
        if cache.exists():
            try:
                self._cache = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                self._cache = {}

    def _save(self):
        self.cache_path.parent.mkdir(exist_ok=True)
        tmp = self.cache_path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cache_path)

    def prime(self, lead_texts: list[str]) -> None:
        """Одна пачка вводных фраз на документ, максимум 40 фраз на вызов."""
        todo = []
        seen = set()
        for t in lead_texts:
            k = _key(t)
            if k not in self._cache and k not in seen:
                seen.add(k)
                todo.append(t)
        for i in range(0, len(todo), 40):
            batch = todo[i : i + 40]
            user = (
                "Для каждой фразы определи, вводит ли она перечень функций, прав или запретов конкретной роли "
                "или подразделения, кто владелец этого перечня и какая у него модальность. Владельца называй "
                "коротко и канонично: «Директор ДККМ», «Главный аудитор», «Работники БВА», «Директоры департаментов». "
                "Если фраза вводит перечень, но роль в ней не названа (например «организуют работу, обеспечивая:»), "
                "это вложенная фраза, is_role_heading = false. Заголовки разделов и обычные предложения тоже false.\n\n"
                + "\n".join(f"[{j}] {t}" for j, t in enumerate(batch))
            )
            kwargs = {} if self.model.startswith(("gpt-5", "o")) else {"temperature": 0}
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                response_format={"type": "json_schema", "json_schema": {"name": "leads", "strict": True, "schema": SCHEMA}},
                **kwargs,
            )
            self.calls += 1
            data = json.loads(resp.choices[0].message.content)
            for it in data["items"]:
                if 0 <= it["index"] < len(batch):
                    self._cache[_key(batch[it["index"]])] = {
                        "role": bool(it["is_role_heading"]),
                        "owner": it["owner"].strip() or None,
                        "modality": it["modality"] if it["modality"] != "none" else None,
                    }
        if todo:
            self._save()

    def __call__(self, text: str):
        """(role_like, owner, modality | None). Для фразы вне кеша падаем на эвристику."""
        d = self._cache.get(_key(text))
        if d is None:
            from .parse import is_role_like, modality_in, owner_of

            rl = is_role_like(text)
            return rl, (owner_of(text) if rl else None), modality_in(text)
        return d["role"], (d["owner"] if d["role"] else None), d["modality"]
