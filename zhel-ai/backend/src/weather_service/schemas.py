"""Контракт сервиса погоды: по этим моделям строится Swagger.

Строка ``/nwp`` совпадает по полям с ``WeatherRow`` сервиса модели dev2 и уходит в ``POST /predict``
как есть. Поле, которое уже читают backend или ML-сервис, нельзя переименовать или удалить молча.
Все моменты времени — UTC, в JSON строкой ISO 8601 с ``Z``.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.weather_service.catalog import SourceKind

RunStatus = Literal["used", "stale", "after_as_of"]
TurbineName = Literal["T1", "T2"]


# --- ошибки ---------------------------------------------------------------


class ErrorBody(BaseModel):
    code: str = Field(description="NO_RUN_AVAILABLE, UNKNOWN_SOURCE, VALIDATION_ERROR, LEAKAGE_GUARD, DATA_UNAVAILABLE, INTERNAL_ERROR")
    message: str
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


# --- /health --------------------------------------------------------------


class SourceHealth(BaseModel):
    runs: int = Field(description="Прогонов в кэше")
    valid_from_utc: datetime | None = Field(description="Первый час в кэше, null — кэш пуст")
    valid_to_utc: datetime | None = Field(description="Последний час в кэше")


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="degraded — кэш какого-то источника или SCADA пуст")
    sources: dict[str, SourceHealth]
    scada_rows: int = Field(description="Часов SCADA по обеим турбинам, 0 — SCADA не загружена")


# --- /sources -------------------------------------------------------------


class EnsembleMember(BaseModel):
    source: str
    weight: float = Field(description="Вес в среднем. Если модели нет, веса остальных пересчитываются поровну")


class WindMae(BaseModel):
    d1: float = Field(description="MAE ветра против SCADA на +24…29 ч, м/с")
    d2: float = Field(description="MAE ветра против SCADA на +48…53 ч, м/с")


class SourceInfo(BaseModel):
    source: str = Field(description="Имя для параметра source")
    kind: SourceKind = Field(description="single_runs — точные прогоны, previous_runs — прогон по правилу previous_dayN, ensemble — среднее моделей")
    title: str
    run_step_h: int | None = Field(description="Шаг прогонов, ч. У ensemble null")
    publication_delay_h: float | None = Field(
        description="Прогон считается доступным через столько часов после запуска. У ensemble null: у каждой модели своя"
    )
    delay_confirmed: bool | None = Field(description="false — задержка принята с запасом без документированного времени публикации, это допущение")
    delay_basis: str | None = Field(description="На чем основана задержка")
    wind_heights_m: list[int] = Field(description="Высоты, на которых в кэше есть скорость ветра")
    empty_columns: list[str] = Field(description="Колонки строки /nwp, которые у источника всегда null")
    archive_from_utc: datetime | None = Field(description="Первый час в кэше")
    archive_to_utc: datetime | None = Field(description="Последний час в кэше")
    members: list[EnsembleMember] | None = Field(description="Состав ансамбля, у остальных источников null")
    wind_mae_ms: WindMae | None = Field(
        description="Точность ветра из reports/weather_vs_scada.md, отложенный период после 01.08.2025. null — не оценивалась"
    )


# --- /nwp -----------------------------------------------------------------


def _speed(description: str) -> float | None:
    return Field(default=None, description=f"{description}, м/с")


class NwpRow(BaseModel):
    """Прогноз одного источника на один час."""

    valid_time_utc: datetime = Field(description="Час, к которому относятся значения, начало часа")
    source: str
    run_init_utc: datetime = Field(description="Запуск прогона. У ensemble — самый поздний из прогонов участников")
    available_at_utc: datetime = Field(description="Когда прогон стал доступен, всегда не позже as_of. У ensemble — самый поздний из участников")
    lead_h: int = Field(description="Часы от запуска прогона до valid_time_utc, а не от выпуска")
    ws10: float | None = _speed("Ветер на 10 м, не загружается ни у одного источника")
    ws80: float | None = _speed("Ветер на 80 м, высота ступицы. У ensemble — среднее ветра на высоте ступицы по моделям")
    ws100: float | None = _speed("Ветер на 100 м")
    ws120: float | None = _speed("Ветер на 120 м")
    wd100: float | None = Field(default=None, description="Направление ветра на 100 м, градусы, откуда дует. У ensemble — векторное среднее")
    gust10: float | None = _speed("Порывы на 10 м")
    t2m: float | None = Field(default=None, description="Температура на 2 м, °C")
    rh2m: float | None = Field(default=None, description="Относительная влажность на 2 м, %")
    psfc: float | None = Field(default=None, description="Давление у поверхности, гПа")
    ws_spread: float | None = _speed(
        "Только у ensemble: стандартное отклонение ветра на высоте ступицы между моделями (ddof=0). null, если модель одна"
    )
    members: list[str] | None = Field(default=None, description="Только у ensemble: модели, давшие значение на этот час")


class MissingSource(BaseModel):
    source: str
    missing_hours: int = Field(description="Сколько часов окна у источника нет")


class NwpResponse(BaseModel):
    as_of_utc: datetime
    from_utc: datetime
    to_utc: datetime
    sources: list[str] = Field(description="Источники, которые попали в ответ")
    missing: list[MissingSource] = Field(description="Запрошенные источники, пропущенные из-за отсутствия прогона")
    rows: list[NwpRow] = Field(description="По строке на час и источник, по возрастанию valid_time_utc")


# --- /runs ----------------------------------------------------------------


class RunInfo(BaseModel):
    source: str
    run_init_utc: datetime
    available_at_utc: datetime
    status: RunStatus | None = Field(
        description="used — прогон дал часы горизонта; stale — опубликован к as_of, но на все его часы есть прогон свежее; "
        "after_as_of — опубликован позже as_of. null в режиме событий (from, to)"
    )
    lead_from_h: int | None = Field(description="Минимальная заблаговременность от запуска прогона по часам горизонта, которые он покрывает")
    lead_to_h: int | None = Field(description="Максимальная заблаговременность")
    hours_used: int | None = Field(description="Сколько часов горизонта взято из этого прогона")


# --- /scada ---------------------------------------------------------------


class ScadaRow(BaseModel):
    time_utc: datetime = Field(description="Начало часа, UTC. Время SCADA уже переведено из UTC+6")
    turbine: TurbineName
    power_norm: float | None = Field(description="Средняя мощность за час, доля номинала 0…1")
    wind_ms: float | None = Field(description="Средний ветер анемометра турбины, м/с")
    temp_c: float | None = Field(description="Средняя температура датчика турбины, °C")
    flag: str = Field(description='"" — чистый час; downtime, curtailment, icing, frozen_sensor, cut_out')
