"""Контракт ML-сервиса: что он принимает и что отдает. По этим моделям строится Swagger.

Имена полей согласованы с docs/api-contract.md: строки погоды приходят в том виде, в каком их
отдает сервис погоды dev3 (`GET /nwp`), а паспорт и метрики модели ложатся на `ModelInfo`
и `BacktestMetrics` backend. Поле, которое backend уже отправляет или читает, нельзя
переименовать или удалить молча. Добавить необязательное поле можно.
"""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Turbine = Literal["T1", "T2", "station"]
ModelKind = Literal["lightgbm_quantile", "baseline_power_curve"]

WIND_SPEED_VARIABLES = ("ws10", "ws80", "ws100", "ws120")
WEATHER_VARIABLES = (*WIND_SPEED_VARIABLES, "wd100", "gust10", "t2m", "rh2m", "psfc")
ALL_TURBINES: tuple[Turbine, ...] = ("T1", "T2", "station")
DEFAULT_TURBINES: list[Turbine] = ["T1", "T2"]
CAPACITY_MW: dict[Turbine, float] = {"T1": 2.5, "T2": 2.5, "station": 5.0}


def _speed(description: str) -> float | None:
    return Field(default=None, ge=0, le=75, description=f"{description}, м/с")


class WeatherRow(BaseModel):
    """Прогноз одной модели погоды на один час: строка из `GET /nwp` сервиса погоды как есть.

    Незнакомые поля игнорируются, чтобы новое поле у сервиса погоды не ломало прогноз.
    """

    model_config = ConfigDict(extra="ignore")

    valid_time_utc: AwareDatetime = Field(description="Час, к которому относятся значения, UTC, начало часа")
    source: str = Field(pattern=r"^[a-z0-9_]{1,40}$", description="Модель погоды, например `ecmwf_ifs` или `gfs_global`")
    run_init_utc: AwareDatetime = Field(description="Время запуска прогона погоды, из которого взяты значения")
    available_at_utc: AwareDatetime = Field(
        description="Когда прогон стал доступен. Должно быть не позже `issue_time_utc`, иначе ответ 422 `LEAKAGE_DETECTED`"
    )
    lead_h: int | None = Field(default=None, description="Не используется: сервис сам считает заблаговременность")
    ws10: float | None = _speed("Ветер на 10 м")
    ws80: float | None = _speed("Ветер на 80 м, высота ступицы")
    ws100: float | None = _speed("Ветер на 100 м")
    ws120: float | None = _speed("Ветер на 120 м")
    wd100: float | None = Field(default=None, ge=0, le=360, description="Направление ветра на 100 м, градусы, откуда дует")
    gust10: float | None = _speed("Порывы на 10 м")
    t2m: float | None = Field(default=None, ge=-70, le=60, description="Температура на 2 м, °C")
    rh2m: float | None = Field(default=None, ge=0, le=100, description="Относительная влажность на 2 м, %")
    psfc: float | None = Field(default=None, ge=400, le=1100, description="Давление у поверхности, гПа")


class PredictOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turbines: list[Turbine] = Field(
        default_factory=lambda: list(DEFAULT_TURBINES),
        min_length=1,
        description="Для чего нужен прогноз: турбины `T1`, `T2`, станция целиком `station`. По умолчанию обе турбины",
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


def _example_row(hour: int, source: str, ws100: float | None, ws80: float | None, t2m: float) -> dict:
    row = {
        "valid_time_utc": f"2026-01-31T{hour:02d}:00:00Z",
        "source": source,
        "run_init_utc": "2026-01-30T18:00:00Z",
        "available_at_utc": "2026-01-31T01:30:00Z" if source == "ecmwf_ifs" else "2026-01-31T01:00:00Z",
        "lead_h": hour - 2,
        "wd100": 255,
        "t2m": t2m,
    }
    row.update({key: value for key, value in (("ws100", ws100), ("ws80", ws80)) if value is not None})
    return row


_PREDICT_EXAMPLE = {
    "request_id": "issue-2026-01-31T02Z-v1",
    "issue_time_utc": "2026-01-31T02:00:00Z",
    "horizon_hours": 2,
    "rows": [
        _example_row(3, "ecmwf_ifs", ws100=8.4, ws80=None, t2m=-6.1),
        _example_row(3, "gfs_global", ws100=None, ws80=7.9, t2m=-5.4),
        _example_row(4, "ecmwf_ifs", ws100=9.1, ws80=None, t2m=-6.4),
        _example_row(4, "gfs_global", ws100=None, ws80=8.3, t2m=-5.9),
    ],
    "options": {"turbines": ["T1", "T2"], "interval_scale": 1.0, "wind_shift_ms": 0.0},
}


class PredictRequest(BaseModel):
    """Запрос прогноза на момент T: погода, доступная к T, на часы T+1 … T+horizon_hours."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [_PREDICT_EXAMPLE]})

    request_id: str | None = Field(default=None, max_length=100, description="Идентификатор вызова. Возвращается в ответе и пишется в лог")
    issue_time_utc: AwareDatetime = Field(description="Момент прогноза T, UTC, ровно начало часа")
    horizon_hours: int = Field(default=48, ge=1, le=48, description="Горизонт: прогноз на часы T+1 … T+horizon_hours")
    rows: list[WeatherRow] = Field(
        min_length=1,
        max_length=5000,
        description="Строки «час × модель погоды» из `GET /nwp`. На каждый час горизонта нужна хотя бы одна строка со скоростью ветра",
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
    turbine: Turbine
    p10: float = Field(ge=0, le=1, description="Мощность, которую факт превысит с вероятностью 90%, доля от номинала")
    p50: float = Field(ge=0, le=1, description="Медианный прогноз, доля от номинала")
    p90: float = Field(ge=0, le=1, description="Мощность, которую факт превысит с вероятностью 10%, доля от номинала")


class HourlyInputs(BaseModel):
    """Что модель увидела на входе в этот час: для графика ветра, проверок агента и отчета."""

    valid_time_utc: AwareDatetime
    lead_h: int = Field(ge=1, le=48)
    wind_speed_hub_ms: float = Field(description="Ветер на высоте ступицы 80 м, среднее по моделям погоды, с учетом `wind_shift_ms`")
    wind_spread_ms: float | None = Field(description="Разброс ветра между моделями погоды, м/с. Пусто, если модель одна")
    t2m: float | None = Field(description="Температура, среднее по моделям погоды, °C")
    sources: list[str] = Field(description="Модели погоды, давшие данные на этот час")


class PredictWarning(BaseModel):
    code: Literal["SOURCE_MISSING", "PARTIAL_SOURCE", "BASELINE_MODEL"] = Field(
        description="`SOURCE_MISSING`: нет модели погоды, на которой училась модель. `PARTIAL_SOURCE`: модель погоды есть не на все часы. "
        "`BASELINE_MODEL`: обученной модели нет, работает физическая кривая мощности"
    )
    message: str
    source: str | None = None


class PredictResponse(BaseModel):
    request_id: str | None
    issue_time_utc: AwareDatetime
    model: ModelRef
    unit: Literal["capacity_fraction"] = Field(description="p10, p50, p90 — доля от номинальной мощности, от 0 до 1")
    capacity_mw: dict[Turbine, float] = Field(description="Номинальная мощность каждой турбины и станции, МВт: умножьте долю на нее")
    forecast: list[ForecastPoint] = Field(description="По строке на каждый час и турбину. Всегда P10 ≤ P50 ≤ P90")
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


class FeatureImportance(BaseModel):
    name: str
    importance: float = Field(ge=0, description="Доля важности, в сумме по признакам 1")
    description: str | None = None


class PowerCurvePoint(BaseModel):
    wind_ms: float = Field(ge=0)
    power_norm: float = Field(ge=0, le=1, description="Доля от номинала")


class ModelInfo(BaseModel):
    """Паспорт модели: страница «Модель». Поля совпадают с `ModelInfo` backend."""

    name: str
    version: str
    kind: ModelKind
    quantiles: list[float]
    trained_until_utc: AwareDatetime | None = None
    training_period_start_utc: AwareDatetime | None = None
    train_rows: int | None = Field(default=None, description="Строк в обучающей выборке")
    walk_forward: str | None = Field(default=None, description="Как устроена проверка: на чем учились и на чем проверяли")
    turbines: list[Turbine]
    capacity_mw: dict[Turbine, float]
    inputs: ModelInputs
    features: list[FeatureImportance] = Field(description="Признаки модели с долей важности")
    power_curve: list[PowerCurvePoint] = Field(description="Кривая мощности, которую использует модель")
    notes: str | None = None


class BaselineScore(BaseModel):
    name: str
    nmae_pct: float


class MetricsDay(BaseModel):
    issue_time_utc: AwareDatetime
    nmae_pct: float
    bias_pct: float


class MetricsLead(BaseModel):
    lead_h: int = Field(ge=1, le=48)
    nmae_pct: float


class MetricsPoint(BaseModel):
    issue_time_utc: AwareDatetime
    valid_time_utc: AwareDatetime
    lead_h: int = Field(ge=1, le=48)
    turbine: Turbine
    p10: float = Field(ge=0, le=1)
    p50: float = Field(ge=0, le=1)
    p90: float = Field(ge=0, le=1)
    actual: float = Field(ge=0, le=1)


class ModelMetrics(BaseModel):
    """Проверка модели на отложенном периоде: страница «Бэктест». Поля совпадают с `BacktestMetrics` backend."""

    model_version: str
    period_start_utc: AwareDatetime
    period_end_utc: AwareDatetime
    nmae_d1_pct: float = Field(description="nMAE на часах lead 1–24, % от номинала")
    nmae_d2_pct: float = Field(description="nMAE на часах lead 25–48, % от номинала")
    nrmse_48_pct: float
    skill_vs_persistence_pct: float
    coverage_p10_p90_pct: float = Field(description="Доля часов, где факт попал в P10–P90, цель около 80")
    baselines: list[BaselineScore] = Field(description="Те же метрики у простых методов для сравнения")
    by_day: list[MetricsDay] = Field(description="По строке на каждый выпуск: календарь запусков")
    by_lead: list[MetricsLead] = Field(description="Ошибка для каждого часа горизонта 1…48")
    series: list[MetricsPoint] = Field(
        default_factory=list, description="Прогноз и факт по часам: график «прогноз против факта» и доля недобора у диспетчера"
    )


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"] = Field(description="`degraded`: обученной модели нет, работает физическая кривая мощности")
    model_loaded: bool = Field(description="Загружена обученная модель")
    model_version: str
    model_kind: ModelKind


class ErrorBody(BaseModel):
    code: str = Field(
        description="VALIDATION_ERROR, ISSUE_TIME_NOT_ON_HOUR, LEAKAGE_DETECTED, INCONSISTENT_RUN_TIMES, DUPLICATE_ROWS, "
        "OUT_OF_HORIZON, INSUFFICIENT_INPUTS, MISSING_REQUIRED_SOURCE, METRICS_NOT_AVAILABLE, INTERNAL_ERROR"
    )
    message: str
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody
