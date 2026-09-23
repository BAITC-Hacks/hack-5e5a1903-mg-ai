"""Формы данных на границе с соседними модулями.

Это граница доверия: всё, что пришло от источника погоды (dev3) и от сервиса
модели (dev2), проверяется схемой, а не разбирается руками. Не прошло проверку —
клиент бросает ``UpstreamError`` с кодом ``*_BAD_RESPONSE``, и агент уходит
на заглушку, а не подставляет выдуманные числа в ответ фронтенду.

Схемы погоды повторяют таблицу ``AsOfStore.get_nwp`` из ``src/forecast/weather``
и ответ ``GET /nwp``, если dev3 однажды поднимет сервис. Схемы модели повторяют
контракт ML-сервиса (``ml/openapi.json``): бэкенд читает только те поля,
на которые опирается, остальные игнорирует, поэтому новое поле у соседа
ничего не ломает.
"""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from src.core.base_schemas import BaseAppSchema


def _to_utc(value: datetime) -> datetime:
    """Время без зоны считаем временем UTC.

    Без этого сравнение ``available_at_utc <= as_of`` уронило бы процесс
    с ``TypeError``, стоит соседу прислать время без суффикса ``Z``.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]


class UpstreamSchema(BaseAppSchema):
    """Ответ соседа: незнакомые поля игнорируются, известные проверяются."""

    model_config = ConfigDict(extra="ignore", from_attributes=True)


# --- погода (dev3) -------------------------------------------------------


class NwpRow(UpstreamSchema):
    """Строка прогноза погоды: один час горизонта одного прогона.

    Скорости ветра необязательны по отдельности, потому что разные модели дают
    разные высоты: у ECMWF 0.25° нет ветра на 80 м, у GEM нет ветра на 100 м.
    Но хотя бы одна высота обязана быть, иначе строка бесполезна.
    """

    valid_time_utc: UtcDatetime
    source: str
    run_init_utc: UtcDatetime
    available_at_utc: UtcDatetime
    lead_h: int = Field(description="Заблаговременность от запуска прогона, не от момента выпуска")
    t2m: float = Field(description="Температура на 2 м, °C: на ней стоит проверка на обледенение")
    ws80: float | None = None
    ws100: float | None = None
    ws120: float | None = None
    wd100: float | None = None
    gust10: float | None = None
    rh2m: float | None = None
    psfc: float | None = None

    @property
    def hub_wind_ms(self) -> float:
        """Ветер на высоте ступицы: 80 м, иначе ближайшая известная высота."""
        for value in (self.ws80, self.ws100, self.ws120):
            if value is not None:
                return value
        raise ValueError("в строке нет скорости ветра ни на одной высоте")

    @model_validator(mode="after")
    def wind_is_known(self) -> "NwpRow":
        if self.ws80 is None and self.ws100 is None and self.ws120 is None:
            raise ValueError("в строке нет скорости ветра ни на одной высоте")
        return self


class RunRow(UpstreamSchema):
    """Событие «прогон стал доступен». По нему агент решает, пересчитывать ли выпуск."""

    source: str
    run_init_utc: UtcDatetime
    available_at_utc: UtcDatetime


# --- модель (dev2) -------------------------------------------------------


class ModelRef(UpstreamSchema):
    name: str
    version: str
    kind: str


class ForecastPoint(UpstreamSchema):
    """Квантили мощности на один час и одну турбину, доли номинала."""

    valid_time_utc: UtcDatetime
    turbine: str
    p10: float = Field(ge=0.0, le=1.0)
    p50: float = Field(ge=0.0, le=1.0)
    p90: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def quantiles_are_ordered(self) -> "ForecastPoint":
        if not self.p10 <= self.p50 <= self.p90:
            raise ValueError("нарушен порядок квантилей: ожидается p10 <= p50 <= p90")
        return self


class PredictWarning(UpstreamSchema):
    """Предупреждение модели: агент кладет его в журнал решений как есть."""

    code: str
    message: str
    source: str | None = None


class PredictResponse(UpstreamSchema):
    """Ответ ``POST /predict`` ML-сервиса в той части, которой пользуется агент."""

    issue_time_utc: UtcDatetime
    model: ModelRef
    capacity_mw: dict[str, float] = Field(default_factory=dict, description="Номинал каждой турбины: доли номинала умножаются на него")
    forecast: list[ForecastPoint] = Field(min_length=1)
    warnings: list[PredictWarning] = Field(default_factory=list)
    degraded: bool = False


class SourceSpec(UpstreamSchema):
    name: str
    required: bool = False


class ModelInputs(UpstreamSchema):
    sources: list[SourceSpec] = Field(default_factory=list, description="Модели погоды, на которых обучена модель. Пусто — подходит любая")
    variables: list[str] = Field(default_factory=list)
    hub_height_m: float | None = None
    max_horizon_hours: int | None = None


class ModelFeature(UpstreamSchema):
    name: str
    importance: float


class ModelPowerCurvePoint(UpstreamSchema):
    wind_ms: float
    power_norm: float


class MlModelInfo(UpstreamSchema):
    """Паспорт модели, ``GET /model-info``."""

    name: str
    version: str
    kind: str
    quantiles: list[float] = Field(default_factory=list)
    trained_until_utc: UtcDatetime | None = None
    train_rows: int | None = None
    walk_forward: str | None = None
    inputs: ModelInputs = Field(default_factory=ModelInputs)
    features: list[ModelFeature] = Field(default_factory=list)
    power_curve: list[ModelPowerCurvePoint] = Field(default_factory=list)


class MetricsBaseline(UpstreamSchema):
    name: str
    nmae_pct: float


class MetricsDay(UpstreamSchema):
    issue_time_utc: UtcDatetime
    nmae_pct: float
    bias_pct: float


class MetricsLead(UpstreamSchema):
    lead_h: int
    nmae_pct: float


class MlModelMetrics(UpstreamSchema):
    """Оценка модели на отложенном периоде, ``GET /metrics``."""

    period_start_utc: UtcDatetime
    period_end_utc: UtcDatetime
    nmae_d1_pct: float
    nmae_d2_pct: float
    nrmse_48_pct: float
    skill_vs_persistence_pct: float
    coverage_p10_p90_pct: float
    baselines: list[MetricsBaseline] = Field(default_factory=list)
    by_day: list[MetricsDay] = Field(default_factory=list)
    by_lead: list[MetricsLead] = Field(default_factory=list)
