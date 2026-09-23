"""Контракт API прогноза: то, что видит фронтенд.

Схемы описывают ровно те данные, которые нужны восьми страницам дашборда.
Сейчас их наполняет заглушка из ``service.py``, позже те же схемы будут
собираться из ответов сервиса погоды (dev3) и сервиса модели (dev2).
Формы полей менять нельзя без согласования: на них завязан фронтенд.
"""

from datetime import date, datetime

from pydantic import Field

from src.core.base_schemas import BaseAppSchema

# --- общее ---------------------------------------------------------------


class Sourced(BaseAppSchema):
    """Ответ, который честно говорит, откуда взяты числа.

    ``stub`` означает синтетические данные заглушки, ``live`` — данные,
    собранные из сервисов погоды и модели. Интерфейс показывает это пользователю,
    чтобы демонстрационные числа нельзя было принять за настоящие.
    """

    data_source: str = Field(default="live", description="stub или live")


class Turbine(BaseAppSchema):
    name: str = Field(description="T1 или T2")
    lat: float
    lon: float
    rated_mw: float


class SiteInfo(BaseAppSchema):
    """Страница «Объект»."""

    name: str
    turbines: list[Turbine]
    capacity_mw: float
    hub_height_m: int
    nwp_point_lat: float
    nwp_point_lon: float
    local_utc_offset_hours: int
    cut_in_ms: float
    rated_ms: float
    cut_out_ms: float


# --- выпуски прогноза ----------------------------------------------------


class IssueSummary(BaseAppSchema):
    """Один выпуск: строка в списке дней и в календаре бэктеста."""

    issue_date: date = Field(description="День выпуска, он же D-1")
    issue_time_utc: datetime
    target_date: date = Field(description="Сутки D, на которые подается заявка")
    version: int = Field(description="Версия прогноза: растет при существенном пересчете")
    status: str = Field(description="ready, stale или failed")
    degraded: bool = Field(description="Прогноз построен на запасном источнике или климатологии")
    source: str = Field(description="Источник погоды, на котором построена версия")
    flagged_hours: int = Field(description="Сколько часов получили хотя бы один флаг риска")


class ForecastHour(BaseAppSchema):
    """Один час горизонта по одной турбине."""

    valid_time_utc: datetime
    valid_time_local: datetime
    lead_h: int = Field(description="Заблаговременность от момента выпуска, 1…48")
    turbine: str
    p10: float = Field(ge=0.0, le=1.0)
    p50: float = Field(ge=0.0, le=1.0)
    p90: float = Field(ge=0.0, le=1.0)
    p50_mw: float
    wind_ms: float
    temp_c: float
    actual: float | None = Field(default=None, description="Факт, если он уже известен")
    source: str
    run_init_utc: datetime | None
    available_at_utc: datetime | None
    flags: list[str] = Field(default_factory=list, description="cut_out_risk, icing_risk, ramp, degraded, source_spread")


class ForecastKpi(BaseAppSchema):
    """Плитки на странице «Обзор»."""

    mean_load_pct: float
    peak_hour_local: datetime
    peak_mw: float
    hours_below_10_pct: int
    expected_error_pct: float
    day_energy_mwh: float


class ForecastResponse(Sourced):
    issue: IssueSummary
    kpi: ForecastKpi
    summary: str = Field(description="Вывод агента на человеческом языке")
    hours: list[ForecastHour]


# --- журнал решений агента ----------------------------------------------


class AgentDecision(BaseAppSchema):
    """Строка журнала на странице «Агент»."""

    as_of_utc: datetime
    step: str = Field(description="fetch_weather, prepare, run_model, forecast, analyze, recompute_on_update")
    decision: str
    reason_code: str
    reason: str
    level: str = Field(description="TOOL, THINK, OK или WARN")


# --- погода --------------------------------------------------------------


class WeatherRun(BaseAppSchema):
    """Прогон NWP и его доступность на момент выпуска."""

    source: str
    run_init_utc: datetime
    available_at_utc: datetime
    status: str = Field(description="used, stale или after_issue")
    lead_from_h: int
    lead_to_h: int


class WeatherModel(BaseAppSchema):
    name: str
    weight: float
    wind_mae_ms: float
    wind_ms: list[float] = Field(description="Ветер по часам горизонта, 48 значений")


class WeatherResponse(Sourced):
    issue_time_utc: datetime
    runs: list[WeatherRun]
    models: list[WeatherModel]
    ensemble_wind_ms: list[float]
    spread_ms: float


# --- заявка диспетчера ---------------------------------------------------


class DispatchHour(BaseAppSchema):
    valid_time_local: datetime
    bid_mw: float
    p10_mw: float
    p50_mw: float
    p90_mw: float


class DispatchKpi(BaseAppSchema):
    """Заявка на сутки и то, как такая же заявка недобирала в бэктесте модели.

    Недобор считается только по часам, где факт известен. Бэктеста нет —
    поля недобора ``None``, а не выдуманное число.
    """

    day_bid_mwh: float
    expected_mwh: float
    shortfall_hours_share: float | None = Field(description="Доля часов бэктеста, где факт ниже заявки при том же риске")
    mean_shortfall_mwh: float | None = Field(description="Средний недобор заявки в бэктесте, МВт·ч за сутки")
    backtest_hours: int = Field(default=0, description="Сколько часов станции бэктеста легло в поля недобора")


class DispatchResponse(Sourced):
    issue_date: date
    risk: float = Field(ge=0.1, le=0.5, description="Допустимый риск недовыработки")
    kpi: DispatchKpi
    hours: list[DispatchHour]


# --- бэктест и модель ----------------------------------------------------


class BacktestDay(BaseAppSchema):
    issue_date: date
    nmae_pct: float
    bias_pct: float


class BacktestLead(BaseAppSchema):
    lead_h: int
    nmae_pct: float


class BacktestBaseline(BaseAppSchema):
    name: str
    nmae_pct: float


class BacktestMetrics(Sourced):
    """Страница «Бэктест». Числа приходят из оценки модели, не из головы."""

    period_start: date
    period_end: date
    nmae_d1_pct: float
    nmae_d2_pct: float
    nrmse_48_pct: float
    skill_vs_persistence_pct: float
    coverage_p10_p90_pct: float
    baselines: list[BacktestBaseline]
    by_day: list[BacktestDay]
    by_lead: list[BacktestLead]


class PowerCurvePoint(BaseAppSchema):
    wind_ms: float
    power_norm: float


class FeatureImportance(BaseAppSchema):
    name: str
    importance: float


class ModelInfo(Sourced):
    """Страница «Модель»."""

    name: str
    quantiles: list[float]
    trained_until: date
    train_rows: int
    features: list[FeatureImportance]
    power_curve: list[PowerCurvePoint]
    walk_forward: str


# --- прогноз по загруженному датасету ------------------------------------


class UploadedFile(BaseAppSchema):
    """Один принятый CSV: сколько строк данных прочитано и чьи это данные."""

    name: str = Field(description="Имя файла, как его прислал пользователь")
    rows: int = Field(description="Строк данных в файле до отбраковки")
    turbine: str = Field(description="Турбина, которой приписан файл")


class UploadDataset(BaseAppSchema):
    """Паспорт загруженного комплекта: что именно легло в основу прогноза.

    ``dropped_rows`` и ``drop_reasons`` показываются пользователю, а не
    проглатываются: он должен видеть, сколько его данных не прошло проверку
    и почему.
    """

    files: list[UploadedFile]
    period_start: datetime = Field(description="Первый час, попавший в выборку")
    period_end: datetime = Field(description="Последний час, попавший в выборку")
    hours: int = Field(description="Часовых наблюдений после усреднения, суммарно по файлам")
    step_minutes: int = Field(description="Шаг исходных строк, определенный по данным")
    dropped_rows: int = Field(description="Строк отброшено при проверке")
    drop_reasons: dict[str, int] = Field(default_factory=dict, description="Код причины отбраковки → сколько строк")


class PowerCurveBin(BaseAppSchema):
    """Корзина ветра эмпирической кривой: медиана и наблюденный разброс.

    ``p10`` и ``p90`` это 10-й и 90-й процентили мощности, наблюденной
    в этой корзине, а не выдуманный коридор вокруг медианы.
    """

    wind_ms: float = Field(description="Центр корзины ветра, шаг 0,5 м/с")
    power_norm: float = Field(ge=0.0, le=1.0, description="Медиана нормированной мощности")
    p10: float = Field(ge=0.0, le=1.0)
    p90: float = Field(ge=0.0, le=1.0)
    samples: int = Field(description="Сколько часов наблюдений попало в корзину")


class UploadWarning(BaseAppSchema):
    """Предупреждение, которое не мешает выпуску, но меняет доверие к нему."""

    code: str
    message: str


class UploadForecastResponse(ForecastResponse):
    """Выпуск, посчитанный по датасету пользователя, а не по нашим данным."""

    data_source: str = Field(default="uploaded", description="Числа посчитаны по загруженному датасету")
    dataset: UploadDataset
    power_curve: list[PowerCurveBin]
    warnings: list[UploadWarning] = Field(default_factory=list)
