"""Близость текстов: лексическая, векторная или гибрид.

- tfidf: символьные n-граммы, работает без сети. Хорошо ловит почти идентичные пункты.
- openai: text-embedding-3-large. Ловит переформулировки, но у него высокий "пол":
  два никак не связанных пункта одного документа дают около 0.3-0.5, поэтому сырой
  косинус нельзя сравнивать с лексическим порогом.
- hybrid: 0.5 * лексика + 0.5 * (эмбеддинг, растянутый от пола до единицы). Так шкала
  остается сопоставимой с лексическими порогами, а переформулировки подтягиваются.

Эмбеддинги кешируются на диске, чтобы не платить за каждый прогон.
"""

from __future__ import annotations

import hashlib
import os
import pickle
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

CACHE = Path(__file__).parent.parent / ".cache" / "emb.pkl"
EMB_MODEL = "text-embedding-3-large"


class Sim:
    def __init__(self, corpus: list[str], mode: str | None = None, emb_floor: float = 0.35):
        has_key = bool(os.environ.get("OPENAI_API_KEY"))
        self.backend = mode or ("hybrid" if has_key else "tfidf")
        if self.backend != "tfidf" and not has_key:
            raise RuntimeError("для режима %s нужен OPENAI_API_KEY" % self.backend)
        self.emb_floor = emb_floor
        self.vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
        self.vec.fit(corpus)
        self.embed_calls = 0
        self.embed_tokens = 0
        if self.backend != "tfidf":
            from .llm import make_client

            self.client = make_client()
            self._cache: dict[str, np.ndarray] = self._load_cache()

    @staticmethod
    def _load_cache() -> dict:
        """Кеш может писаться другим процессом в этот же момент. Битый или недописанный
        файл это не ошибка, а пустой кеш: эмбеддинги просто посчитаются заново."""
        if not CACHE.exists():
            return {}
        for _ in range(3):
            try:
                with open(CACHE, "rb") as fh:
                    return pickle.load(fh)
            except Exception:  # noqa: BLE001
                import time

                time.sleep(0.2)
        return {}

    def _save_cache(self):
        """Атомарно: во временный файл, потом переименование. Читатель никогда не видит
        наполовину записанный файл."""
        import os
        import time

        CACHE.parent.mkdir(exist_ok=True)
        tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(self._cache, fh)
        for _ in range(5):
            try:
                os.replace(tmp, CACHE)
                return
            except PermissionError:
                time.sleep(0.2)
        tmp.unlink(missing_ok=True)

    # --- лексика ---
    def _lex(self, texts: list[str]):
        m = self.vec.transform(texts)
        norms = np.sqrt(m.multiply(m).sum(axis=1)).A1 + 1e-12
        return m.multiply(1 / norms[:, None]).tocsr()

    # --- эмбеддинги ---
    @staticmethod
    def _key(t: str) -> str:
        return hashlib.sha1((EMB_MODEL + "\0" + t).encode("utf-8")).hexdigest()

    def _emb(self, texts: list[str]) -> np.ndarray:
        todo = sorted({t for t in texts if self._key(t) not in self._cache})
        for i in range(0, len(todo), 100):
            batch = todo[i : i + 100]
            resp = self.client.embeddings.create(model=EMB_MODEL, input=batch)
            self.embed_calls += 1
            self.embed_tokens += resp.usage.total_tokens
            for t, d in zip(batch, resp.data, strict=True):
                v = np.array(d.embedding, dtype=np.float32)
                self._cache[self._key(t)] = v / (np.linalg.norm(v) + 1e-12)
        if todo:
            self._save_cache()
        return np.stack([self._cache[self._key(t)] for t in texts])

    def matrix(self, a: list[str], b: list[str], combine: str = "avg") -> np.ndarray:
        """combine="avg" для выравнивания: консервативно, лексика не дает эмбеддингу
        склеить далекие пункты. combine="max" для дублей: переформулированная функция
        должна проходить по одному только смыслу, пол эмбеддинга уже вычтен."""
        lex = (self._lex(a) @ self._lex(b).T).toarray()
        if self.backend == "tfidf":
            return lex
        emb = self._emb(a) @ self._emb(b).T
        emb = np.clip((emb - self.emb_floor) / (1.0 - self.emb_floor), 0.0, 1.0)
        if self.backend == "openai":
            return emb
        return np.maximum(lex, emb) if combine == "max" else 0.5 * lex + 0.5 * emb

    def raw_embedding_matrix(self, a: list[str], b: list[str]) -> np.ndarray:
        """Сырой косинус эмбеддингов, нужен только для калибровки пола."""
        return self._emb(a) @ self._emb(b).T
