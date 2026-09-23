"""Единая точка создания клиента модели и перевода его отказов в понятный текст.

Каждое обращение к модели идёт с явным таймаутом из ORGDIFF_LLM_TIMEOUT (секунды,
по умолчанию 60) и не более чем двумя повторами. Ключ берётся только из окружения.
"""

from __future__ import annotations

import os

DEFAULT_TIMEOUT = 60.0


def llm_timeout() -> float:
    try:
        return float(os.environ.get("ORGDIFF_LLM_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        return DEFAULT_TIMEOUT


def make_client():
    from openai import OpenAI

    return OpenAI(timeout=llm_timeout(), max_retries=2)


def explain_error(e: Exception) -> str | None:
    """Текст для пользователя по типу ошибки модели; None, если это не ошибка модели."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return None
    if isinstance(e, openai.AuthenticationError):
        return "ключ OpenAI отклонён: проверьте OPENAI_API_KEY"
    if isinstance(e, openai.RateLimitError):
        return "исчерпан лимит запросов или средств на ключе OpenAI, повторите позже"
    if isinstance(e, openai.APITimeoutError):
        return f"модель не ответила за {llm_timeout():.0f} с, повторите анализ или увеличьте ORGDIFF_LLM_TIMEOUT"
    if isinstance(e, openai.APIConnectionError):
        return "нет соединения с API OpenAI: проверьте сеть; без ключа анализ идёт по близости текстов"
    if isinstance(e, openai.APIStatusError):
        return f"API OpenAI вернуло ошибку {e.status_code}, повторите анализ позже"
    return None
