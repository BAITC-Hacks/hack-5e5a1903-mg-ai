"""Контракт ML-сервиса: что он принимает и что отдает. По этим моделям строится Swagger.

Меняется только согласованно с backend: поле, которое backend уже отправляет или читает,
нельзя переименовать или удалить молча. Добавить необязательное поле можно.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Target = Literal["T1", "T2", "station"]
ModelKind = Literal["lightgbm_quantile", "baseline_power_curve"]

WIND_SPEED_VARIABLES = ("wind_speed_10m", "wind_speed_80m", "wind_speed_100m", "wind_speed_120m")
WEATHER_VARIABLES = (
    *WIND_SPEED_VARIABLES,
    "wind_direction_10m",
    "wind_direction_80m",
    "wind_direction_100m",
    "wind_direction_120m",
    "wind_gusts_10m",
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
)
ALL_TARGETS: tuple[Target, ...] = ("T1", "T2", "station")
CAPACITY_MW: dict[Target, float] = {"T1": 2.5, "T2": 2.5, "station": 5.0}


def _speed(description: str) -> float | None:
    return Field(default=None, ge=0, le=75, description=f"{description}, м/с")


def _direction(description: str) -> float | None:
    return Field(default=None, ge=0, le=360, description=f"{description}, градусы, откуда дует")


class WeatherRow(BaseModel):
    """Прогноз одной модели погоды на один час. Имена переменных как в Open-Meteo."""

    model_config = ConfigDict(extra="forbid")

    valid_time_utc: AwareDatetime = Field(description="Час, к которому относятся значения, UTC, начало часа")
    source: str = Field(pattern=r"^[a-z0-9_]{1,40}$", description="Модель погоды, например `ecmwf_ifs025` или `gfs_global`")
    run_init_utc: AwareDatetime = Field(description="Время запуска прогона погоды, из которого взяты значения")
    available_at_utc: AwareDatetime = Field(
        description="Когда прогон стал доступен. Должно быть не позже `issue_time_utc`, иначе ответ 422 `LEAKAGE_DETECTED`"
    )
    wind_speed_10m: float | None = _speed("Ветер на 10 м")
    wind_speed_80m: float | None = _speed("Ветер на 80 м, высота ступицы")
    wind_speed_100m: float | None = _speed("Ветер на 100 м")
    wind_speed_120m: float | None = _speed("Ветер на 120 м")
    wind_direction_10m: float | None = _direction("Направление ветра на 10 м")
    wind_direction_80m: float | None = _direction("Направление ветра на 80 м")
    wind_direction_100m: float | None = _direction("Направление ветра на 100 м")
    wind_direction_120m: float | None = _direction("Направление ветра на 120 м")
    wind_gusts_10m: float | None = _speed("Порывы на 10 м")
    temperature_2m: float | None = Field(default=None, ge=-70, le=60, description="Температура на 2 м, °C")
    relative_humidity_2m: float | None = Field(default=None, ge=0, le=100, description="Относительная влажность на 2 м, %")
    surface_pressure: float | None = Field(default=None, ge=400, le=1100, description="Давление у поверхности, гПа")


class PredictOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    targets: list[Target] = Field(
        default_factory=lambda: list(ALL_TARGETS),
        min_length=1,
        description="Для чего нужен прогноз: турбины `T1`, `T2` и станция целиком `station`",
    )
    interval_scale: float = Field(
        default=1.0,
        ge=1.0,
        le=3.0,
        description="Во сколько раз расширить интервал P10–P90 вокруг P50. Агент ставит больше 1, когда модели погоды расходятся",
    )
    wind_shift_ms: float = Field(
        default=0.0,
        ge=-5.0,
        le=5.0,
        description="Сценарий «что если»: сдвиг всех скоростей ветра и порывов, м/с. Для обычного прогноза 0",
    )


_PREDICT_EXAMPLE = {
    "request_id": "issue-2026-01-31T02Z-v1",
    "issue_time_utc": "2026-01-31T02:00:00Z",
    "horizon_hours": 2,
    "weather": [
        {
            "valid_time_utc": "2026-01-31T03:00:00Z",
            "source": "ecmwf_ifs025",
            "run_init_utc": "2026-01-30T18:00:00Z",
            "available_at_utc": "2026-01-31T01:30:00Z",
            "wind_speed_100m": 8.4,
            "wind_direction_100m": 255,
            "wind_gusts_10m": 11.2,
            "temperature_2m": -6.1,
            "relative_humidity_2m": 78,
            "surface_pressure": 912.5,
        },
        {
            "valid_time_utc": "2026-01-31T03:00:00Z",
            "source": "gfs_global",
            "run_init_utc": "2026-01-30T18:00:00Z",
            "available_at_utc": "2026-01-31T01:00:00Z",
            "wind_speed_80m": 7.9,
            "wind_direction_80m": 250,
            "temperature_2m": -5.4,
        },
        {
            "valid_time_utc": "2026-01-31T04:00:00Z",
            "source": "ecmwf_ifs025",
            "run_init_utc": "2026-01-30T18:00:00Z",
            "available_at_utc": "2026-01-31T01:30:00Z",
            "wind_speed_100m": 9.1,
            "wind_direction_100m": 258,
            "wind_gusts_10m": 12.0,
            "temperature_2m": -6.4,
            "relative_humidity_2m": 80,
            "surface_pressure": 912.8,
        },
        {
            "valid_time_utc": "2026-01-31T04:00:00Z",
            "source": "gfs_global",
            "run_init_utc": "2026-01-30T18:00:00Z",
            "available_at_utc": "2026-01-31T01:00:00Z",
            "wind_speed_80m": 8.3,
            "wind_direction_80m": 252,
            "temperature_2m": -5.9,
        },
    ],
    "options": {"targets": ["T1", "T2", "station"], "interval_scale": 1.0, "wind_shift_ms": 0.0},
}


class PredictRequest(BaseModel):
    """Запрос прогноза на момент T: погода, доступная к T, на часы T+1 … T+horizon_hours."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_PREDICT_EXAMPLE]})

    request_id: str | None = Field(default=None, max_length=100, description="Идентификатор вызова. Возвращается в ответе и пишется в лог")
    issue_time_utc: AwareDatetime = Field(description="Момент прогноза T, UTC, ровно начало часа")
    horizon_hours: int = Field(default=48, ge=1, le=48, description="Горизонт: прогноз на часы T+1 … T+horizon_hours")
    weather: list[WeatherRow] = Field(
        min_length=1,
        max_length=5000,
        description="Строки «час × модель погоды». На каждый час горизонта нужна хотя бы одна строка со скоростью ветра",
    )
    options: PredictOptions = Field(default_factory=PredictOptions)


class ModelRef(BaseModel):
    name: str
    version: str
    kind: ModelKind
    trained_until_utc: AwareDatetime | None = Field(default=None, description="Самые свежие данные, которые видела модель при обучении")


class ForecastPoint(BaseModel):
    valid_time_utc: AwareDatetime
    lead_h: int = Field(ge=1, le=48, description="Часы от момента прогноза")
    target: Target
    p10: float = Field(ge=0, le=1, description="Мощность, которую факт превысит с вероятностью 90%, доля от номинала")
    p50: float = Field(ge=0, le=1, description="Медианный прогноз, доля от номинала")
    p90: float = Field(ge=0, le=1, description="Мощность, которую факт превысит с вероятностью 10%, доля от номинала")


class HourlyInputs(BaseModel):
    """Что модель увидела на входе в этот час: для графика ветра, проверок агента и отчета."""

    valid_time_utc: AwareDatetime
    lead_h: int = Field(ge=1, le=48)
    wind_speed_hub_ms: float = Field(description="Ветер на высоте ступицы 80 м, среднее по моделям погоды, с учетом `wind_shift_ms`")
    wind_spread_ms: float | None = Field(description="Разброс ветра между моделями погоды, м/с. Пусто, если модель одна")
    temperature_2m: float | None = Field(description="Температура, среднее по моделям погоды, °C")
    sources: list[str] = Field(description="Модели погоды, давшие данные на этот час")


class PredictWarning(BaseModel):
    code: Literal["SOURCE_MISSING", "PARTIAL_SOURCE", "BASELINE_MODEL"] = Field(
        description="`SOURCE_MISSING`: нет модели погоды, на которой учились. `PARTIAL_SOURCE`: модель есть не на все часы. "
        "`BASELINE_MODEL`: обученной модели нет, работает физическая кривая мощности"
    )
    message: str
    source: str | None = None


class PredictResponse(BaseModel):
    request_id: str | None
    issue_time_utc: AwareDatetime
    model: ModelRef
    unit: Literal["capacity_fraction"] = Field(description="p10, p50, p90 — доля от номинальной мощности цели, от 0 до 1")
    capacity_mw: dict[Target, float] = Field(description="Номинальная мощность каждой цели, МВт: умножьте долю на нее")
    forecast: list[ForecastPoint] = Field(description="По строке на каждый час и цель. Всегда P10 ≤ P50 ≤ P90")
    hourly_inputs: list[HourlyInputs]
    warnings: list[PredictWarning]
    degraded: bool = Field(description="Прогноз построен не в полной конфигурации: нет части моделей погоды или обученной модели")
    inference_ms: float


class SourceSpec(BaseModel):
    name: str
    required: bool = Field(description="Без обязательной модели погоды прогноз не строится: ответ 422 `MISSING_REQUIRED_SOURCE`")


class ModelInputs(BaseModel):
    sources: list[SourceSpec] = Field(description="Модели погоды, на которых обучена модель. Пустой список: подходит любая")
    variables: list[str] = Field(description="Переменные погоды, которые модель использует")
    hub_height_m: float
    max_horizon_hours: int


class FeatureSpec(BaseModel):
    name: str
    description: str


class FeatureImportance(BaseModel):
    feature: str
    importance: float = Field(ge=0, description="Доля важности, в сумме по признакам 1")


class PowerCurvePoint(BaseModel):
    wind_speed_ms: float = Field(ge=0)
    power: float = Field(ge=0, le=1, description="Доля от номинала")


class BaselineScore(BaseModel):
    name: str
    nmae_pct: float


class MetricsSummary(BaseModel):
    period_start_utc: AwareDatetime
    period_end_utc: AwareDatetime
    nmae_pct_lead_1_24: float | None = None
    nmae_pct_lead_25_48: float | None = None
    nmae_pct_day_ahead: float | None = Field(default=None, description="Сутки D из выпуска D-1")
    nrmse_pct_48h: float | None = None
    coverage_p10_p90_pct: float | None = Field(default=None, description="Доля часов, где факт попал в P10–P90, цель около 80")
    nmae_pct_by_lead: list[float] = Field(default_factory=list, description="nMAE для каждого часа горизонта 1…48")
    baselines: list[BaselineScore] = Field(default_factory=list, description="Те же метрики у простых методов для сравнения")


class ModelCard(BaseModel):
    """Паспорт модели: страница «Модель» и строка «Ожидаемая ошибка» на дашборде."""

    name: str
    version: str
    kind: ModelKind
    created_at_utc: AwareDatetime | None = None
    trained_until_utc: AwareDatetime | None = None
    training_period_start_utc: AwareDatetime | None = None
    quantiles: list[float]
    targets: list[Target]
    capacity_mw: dict[Target, float]
    inputs: ModelInputs
    features: list[FeatureSpec]
    feature_importance: list[FeatureImportance]
    power_curve: list[PowerCurvePoint] = Field(description="Кривая мощности, которую использует модель")
    metrics: MetricsSummary | None = Field(default=None, description="Качество на отложенном периоде. Пусто, пока модель не обучена")
    notes: str | None = None


class BacktestIssue(BaseModel):
    issue_time_utc: AwareDatetime
    nmae_pct_lead_1_24: float
    nmae_pct_lead_25_48: float
    nmae_pct_48h: float
    bias_pct: float


class BacktestPoint(BaseModel):
    issue_time_utc: AwareDatetime
    valid_time_utc: AwareDatetime
    lead_h: int = Field(ge=1, le=48)
    target: Target
    p10: float = Field(ge=0, le=1)
    p50: float = Field(ge=0, le=1)
    p90: float = Field(ge=0, le=1)
    actual: float = Field(ge=0, le=1)


class BacktestReport(BaseModel):
    """Проверка модели на отложенном периоде: страницы «Бэктест» и «Диспетчер»."""

    model_version: str
    summary: MetricsSummary
    issues: list[BacktestIssue] = Field(description="По строке на каждый выпуск: календарь запусков")
    series: list[BacktestPoint] = Field(description="Прогноз и факт по часам: график «прогноз против факта» и доля недобора")


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="`degraded`: обученной модели нет, работает физическая кривая мощности")
    model_loaded: bool = Field(description="Загружена обученная модель")
    model_version: str
    model_kind: ModelKind


class ErrorBody(BaseModel):
    code: str = Field(
        description="VALIDATION_ERROR, ISSUE_TIME_NOT_ON_HOUR, LEAKAGE_DETECTED, INCONSISTENT_RUN_TIMES, DUPLICATE_ROWS, "
        "OUT_OF_HORIZON, INSUFFICIENT_INPUTS, MISSING_REQUIRED_SOURCE, BACKTEST_NOT_AVAILABLE, INTERNAL_ERROR"
    )
    message: str
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
