"""Общая часть HTTP-клиентов соседних сервисов.

Правила границы взяты из ``docs/api-contract.md`` и
``docs/architecture-guidelines.md``:

- у каждого вызова наружу явный таймаут из переменной окружения;
- отказ соседа превращается в ``BusinessError`` с понятным кодом, а не в 500
  и не в пустой ответ;
- разные причины отказа дают разные коды: ``*_TIMEOUT``, ``*_UNAVAILABLE``,
  ``*_BAD_RESPONSE``;
- ответ проверяется схемой Pydantic, а не разбирается руками.

Состояния в памяти процесса тут нет: ``httpx.AsyncClient`` живет ровно один
вызов, поэтому реплики бэкенда остаются взаимозаменяемыми.
"""

import logging
from datetime import UTC, datetime
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from src.core.exceptions import BusinessError

logger = logging.getLogger(__name__)

RowT = TypeVar("RowT", bound=BaseModel)

REASON_TIMEOUT = "TIMEOUT"
REASON_UNAVAILABLE = "UNAVAILABLE"
REASON_BAD_RESPONSE = "BAD_RESPONSE"
REASON_REJECTED = "REJECTED"


def _upstream_code(response: httpx.Response) -> str | None:
    """Код ошибки соседа из общего конверта ``{"error": {"code", ...}}``."""
    try:
        payload = response.json()
    except ValueError:
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    return error.get("code") if isinstance(error, dict) else None


def iso_utc(value: datetime) -> str:
    """Время для параметров запроса: всегда UTC и всегда с суффиксом ``Z``."""
    moment = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return moment.isoformat().replace("+00:00", "Z")


class UpstreamError(BusinessError):
    """Отказ соседнего сервиса.

    Отдельный тип нужен затем, чтобы агент отличал «сосед не ответил» от
    остальных ошибок: первое переводит выпуск на заглушку, второе так и уходит
    клиенту. Код вида ``WEATHER_TIMEOUT`` говорит и чей сервис отказал,
    и что именно случилось.
    """


class UpstreamClient:
    """База для клиента соседнего сервиса: один запрос — один разобранный ответ."""

    service = "UPSTREAM"
    title = "Соседний сервис"

    def __init__(self, base_url: str, timeout: float, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Транспорт подменяется в тестах на httpx.MockTransport, поднимать
        # настоящие сервисы соседей ради теста не нужно.
        self._transport = transport

    def error(self, reason: str, status_code: int, message: str, details: dict | None = None) -> UpstreamError:
        code = f"{self.service}_{reason}"
        logger.warning("%s: %s (%s)", self.title, message, code)
        return UpstreamError(status_code=status_code, code=code, message=message, details=details)

    async def fetch(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
        """Вызов наружу. Любой отказ становится ``UpstreamError``, а не 500."""
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                transport=self._transport,
            ) as client:
                response = await client.request(method, path, params=params, json=json)
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException as error:
            raise self.error(
                REASON_TIMEOUT,
                504,
                f"{self.title} не ответил за {self.timeout} с",
                {"path": path, "timeout_s": self.timeout},
            ) from error
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            upstream_code = _upstream_code(error.response)
            if status < 500:
                # Сосед ответил и отказал осознанно: повтор не поможет, причина
                # известна по его коду, например METRICS_NOT_AVAILABLE.
                raise self.error(
                    REASON_REJECTED,
                    502,
                    f"{self.title} отклонил запрос: {upstream_code or status}",
                    {"path": path, "status": status, "upstream_code": upstream_code},
                ) from error
            raise self.error(
                REASON_UNAVAILABLE,
                503,
                f"{self.title} ответил кодом {status}",
                {"path": path, "status": status, "upstream_code": upstream_code},
            ) from error
        except httpx.HTTPError as error:
            raise self.error(
                REASON_UNAVAILABLE,
                503,
                f"{self.title} недоступен по адресу {self.base_url}",
                {"path": path},
            ) from error
        except ValueError as error:
            raise self.error(
                REASON_BAD_RESPONSE,
                502,
                f"{self.title} вернул не JSON",
                {"path": path},
            ) from error

    async def fetch_rows(self, row: type[RowT], method: str, path: str, *, params: dict | None = None, json: Any = None) -> list[RowT]:
        """Тот же вызов, но ответ обязан лечь в список схем."""
        payload = await self.fetch(method, path, params=params, json=json)
        return self.validate(list[row], payload, path)

    async def fetch_one(self, model: type[RowT], method: str, path: str, *, params: dict | None = None, json: Any = None) -> RowT:
        """Тот же вызов, но ответ обязан лечь в одну схему."""
        payload = await self.fetch(method, path, params=params, json=json)
        return self.validate(model, payload, path)

    def validate(self, shape: Any, payload: Any, path: str) -> Any:
        """Проверка схемой. Ответ не по контракту это ``*_BAD_RESPONSE``, а не 500."""
        try:
            return TypeAdapter(shape).validate_python(payload)
        except ValidationError as error:
            raise self.error(
                REASON_BAD_RESPONSE,
                502,
                f"{self.title} вернул ответ не по контракту",
                {"path": path, "errors": error.error_count(), "first_error": str(error.errors()[0]["msg"])},
            ) from error
