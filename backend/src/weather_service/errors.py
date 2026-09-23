"""Ошибки сервиса погоды. Клиент получает конверт ``{"error": {"code", "message", "details"}}``, как в backend."""

from typing import Any


class WeatherServiceError(Exception):
    """Ожидаемая ошибка с HTTP-статусом и машиночитаемым кодом."""

    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


def no_run_available(source: str | list[str], as_of: str, missing: list[str], missing_hours: int) -> WeatherServiceError:
    return WeatherServiceError(
        404,
        "NO_RUN_AVAILABLE",
        "На момент as_of нет опубликованного прогона хотя бы для одного из запрошенных часов",
        {"source": source, "as_of_utc": as_of, "missing_hours": missing_hours, "examples": missing},
    )


def unknown_source(names: list[str], known: list[str]) -> WeatherServiceError:
    return WeatherServiceError(422, "UNKNOWN_SOURCE", f"Неизвестный источник погоды: {', '.join(names)}", {"unknown": names, "known": known})


def validation_error(message: str, details: dict[str, Any] | None = None) -> WeatherServiceError:
    return WeatherServiceError(422, "VALIDATION_ERROR", message, details)


def leakage_guard(message: str) -> WeatherServiceError:
    return WeatherServiceError(500, "LEAKAGE_GUARD", f"Ответ остановлен последней проверкой на утечку будущего: {message}")


def data_unavailable(message: str) -> WeatherServiceError:
    return WeatherServiceError(503, "DATA_UNAVAILABLE", message)
