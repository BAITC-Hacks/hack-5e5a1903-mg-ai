"""Копии схем тех, кто читает сервис погоды: по ним тест совместимости проверяет ответы.

Чужие пакеты не импортируются намеренно. Тест должен упасть, когда сервис погоды разойдется
с тем, что backend и ML-сервис приняли на момент копирования, а не подстроиться под их правку.
Поменялась схема у соседа — копия обновляется здесь вместе с проверкой, что сервис ей отвечает.

- ``NwpRow``, ``RunRow``: ``backend/src/modules/forecast/clients/schemas.py`` (dev1),
  ``NwpResponse`` — там же, ответ ``GET /nwp``.
- ``WeatherRow``: ``ml/src/ml_service/schemas.py`` (dev2), строка ``POST /predict``.
- ``UPSTREAM_NO_RUN``: ``backend/src/modules/forecast/clients/weather.py``.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

UPSTREAM_NO_RUN = "NO_RUN_AVAILABLE"


# --- backend dev1 -------------------------------------------------------------


def _to_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]


class UpstreamSchema(BaseModel):
    model_config = ConfigDict(extra="ignore", from_attributes=True)


class NwpRow(UpstreamSchema):
    valid_time_utc: UtcDatetime
    source: str
    run_init_utc: UtcDatetime
    available_at_utc: UtcDatetime
    lead_h: int
    t2m: float
    ws80: float | None = None
    ws100: float | None = None
    ws120: float | None = None
    wd100: float | None = None
    gust10: float | None = None
    rh2m: float | None = None
    psfc: float | None = None

    @model_validator(mode="after")
    def wind_is_known(self) -> "NwpRow":
        if self.ws80 is None and self.ws100 is None and self.ws120 is None:
            raise ValueError("в строке нет скорости ветра ни на одной высоте")
        return self


class NwpResponse(UpstreamSchema):
    rows: list[NwpRow]


class RunRow(UpstreamSchema):
    source: str
    run_init_utc: UtcDatetime
    available_at_utc: UtcDatetime


# --- ML-сервис dev2 -----------------------------------------------------------


def _speed() -> float | None:
    return Field(default=None, ge=0, le=75)


class WeatherRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    valid_time_utc: AwareDatetime
    source: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    run_init_utc: AwareDatetime
    available_at_utc: AwareDatetime
    lead_h: int | None = None
    ws10: float | None = _speed()
    ws80: float | None = _speed()
    ws100: float | None = _speed()
    ws120: float | None = _speed()
    wd100: float | None = Field(default=None, ge=0, le=360)
    gust10: float | None = _speed()
    t2m: float | None = Field(default=None, ge=-70, le=60)
    rh2m: float | None = Field(default=None, ge=0, le=100)
    psfc: float | None = Field(default=None, ge=400, le=1100)
