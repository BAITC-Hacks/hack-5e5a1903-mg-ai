"""Клиент ML-сервиса (dev2).

Адрес и таймаут приходят из ``ML_SERVICE_URL`` и ``ML_SERVICE_TIMEOUT``.
Контракт описан в ``ml/openapi.json`` и повторен схемами в ``schemas.py``:
на вход идут строки погоды на момент выпуска в том виде, в каком их отдает
источник погоды, на выходе P10/P50/P90 по каждому часу и каждой турбине.

Прогноз считает модель, а не бэкенд: здесь нет ни кривой мощности, ни правок
квантилей. Бэкенд только собирает запрос, проверяет ответ схемой и передает
предупреждения модели в журнал решений агента.

Запрос ML-сервиса не принимает незнакомых полей, поэтому тело собирается явно,
а не выгрузкой наших схем целиком.
"""

from datetime import datetime

import httpx

from src.core.config import settings
from src.modules.forecast.clients.base import UpstreamClient, iso_utc
from src.modules.forecast.clients.schemas import MlModelInfo, MlModelMetrics, NwpRow, PredictResponse

#: Поля строки погоды, которые понимает ML-сервис.
WEATHER_FIELDS = (
    "valid_time_utc",
    "source",
    "run_init_utc",
    "available_at_utc",
    "lead_h",
    "ws80",
    "ws100",
    "ws120",
    "wd100",
    "gust10",
    "t2m",
    "rh2m",
    "psfc",
)


class MlClient(UpstreamClient):
    service = "ML"
    title = "Сервис модели"

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url if base_url is not None else settings.ML_SERVICE_URL,
            timeout=timeout if timeout is not None else settings.ML_SERVICE_TIMEOUT,
            transport=transport,
        )

    async def predict(
        self,
        *,
        issue_time_utc: datetime,
        rows: list[NwpRow],
        turbines: list[str],
        interval_scale: float = 1.0,
        request_id: str | None = None,
    ) -> PredictResponse:
        """Квантили мощности по строкам погоды.

        ``interval_scale`` больше единицы агент ставит сам, когда модели погоды
        расходятся: интервал P10…P90 в таком выпуске должен быть шире.
        """
        horizon_hours = max(1, len({row.valid_time_utc for row in rows}))
        body = {
            "request_id": request_id,
            "issue_time_utc": iso_utc(issue_time_utc),
            "horizon_hours": min(48, horizon_hours),
            "rows": [_weather_payload(row) for row in rows],
            "options": {"turbines": turbines, "interval_scale": interval_scale, "wind_shift_ms": 0.0},
        }
        return await self.fetch_one(PredictResponse, "POST", "/predict", json=body)

    async def model_info(self) -> MlModelInfo:
        """Паспорт модели: имя, квантили, признаки и источники погоды обучения."""
        return await self.fetch_one(MlModelInfo, "GET", "/model-info")

    async def metrics(self) -> MlModelMetrics:
        """Оценка модели на отложенном периоде.

        Пока модель не оценена, сервис отвечает 404 ``METRICS_NOT_AVAILABLE``,
        и это честный ответ, а не отказ: см. ``ML_REJECTED`` в ``base.py``.
        """
        return await self.fetch_one(MlModelMetrics, "GET", "/metrics")


def _weather_payload(row: NwpRow) -> dict:
    """Строка погоды в том виде, в каком ее ждет ML-сервис."""
    payload = row.model_dump(mode="json", include=set(WEATHER_FIELDS))
    return {key: value for key, value in payload.items() if value is not None}
