"""Наполнение контракта прогноза.

**Сейчас это заглушка.** Числа считаются детерминированно из даты выпуска,
поэтому один и тот же день всегда дает один и тот же ответ, но к реальным
данным они отношения не имеют. Каждый ответ помечен ``data_source="stub"``,
чтобы это было видно и в API, и в интерфейсе.

Когда появятся сервисы погоды (dev3) и модели (dev2), функции ниже заменяются
на сборку из их ответов, а схемы в ``schemas.py`` остаются прежними: на них
завязан фронтенд.

Состояния в памяти процесса здесь нет: все функции чистые, репликам бэкенда
нечего делить.
"""

import hashlib
import math
import random
from datetime import UTC, date, datetime, timedelta

from src.core.exceptions import BusinessError
from src.modules.forecast.schemas import (
    AgentDecision,
    BacktestBaseline,
    BacktestDay,
    BacktestLead,
    BacktestMetrics,
    DispatchHour,
    DispatchKpi,
    DispatchResponse,
    FeatureImportance,
    ForecastHour,
    ForecastKpi,
    ForecastResponse,
    IssueSummary,
    ModelInfo,
    PowerCurvePoint,
    SiteInfo,
    Turbine,
    WeatherModel,
    WeatherResponse,
    WeatherRun,
)

DATA_SOURCE_STUB = "stub"

# Объект: ВЭС «Шелек», две турбины Goldwind GW109/2500.
TURBINES = (
    Turbine(name="T1", lat=43.645150, lon=78.535604, rated_mw=2.5),
    Turbine(name="T2", lat=43.643198, lon=78.538828, rated_mw=2.5),
)
NWP_POINT = (43.6442, 78.5372)
HUB_HEIGHT_M = 80
CUT_IN_MS = 3.0
RATED_MS = 10.3
CUT_OUT_MS = 25.0
LOCAL_OFFSET = timedelta(hours=5)

# Ретро-симуляция: 28 выпусков, каждый в 07:00 по Астане накануне целевых суток.
FIRST_ISSUE = date(2026, 1, 31)
LAST_ISSUE = date(2026, 2, 27)
ISSUE_HOUR_UTC = 2
HORIZON_HOURS = 48

SOURCE_IFS = "ecmwf_ifs"
SOURCE_GFS = "gfs_global"
WEATHER_MODELS = ("ECMWF IFS", "GFS", "ICON", "GEM")


def issue_dates() -> list[date]:
    """Все дни выпуска ретро-симуляции."""
    span = (LAST_ISSUE - FIRST_ISSUE).days
    return [FIRST_ISSUE + timedelta(days=offset) for offset in range(span + 1)]


def issue_time(issue_date: date) -> datetime:
    return datetime(issue_date.year, issue_date.month, issue_date.day, ISSUE_HOUR_UTC, tzinfo=UTC)


def ensure_known_issue(issue_date: date) -> None:
    """Дата вне ретро-симуляции это ошибка клиента, а не пустой ответ."""
    if issue_date < FIRST_ISSUE or issue_date > LAST_ISSUE:
        raise BusinessError(
            404,
            "ISSUE_NOT_FOUND",
            f"Выпуска за {issue_date.isoformat()} нет: симуляция идет с {FIRST_ISSUE.isoformat()} по {LAST_ISSUE.isoformat()}",
        )


def _rng(issue_date: date, salt: str = "") -> random.Random:
    """Генератор, привязанный к дате: ответ воспроизводится при каждом запросе."""
    digest = hashlib.sha256(f"{issue_date.isoformat()}:{salt}".encode()).hexdigest()
    return random.Random(int(digest[:16], 16))


def power_curve(wind_ms: float) -> float:
    """Паспортная кривая GW109 в долях номинала."""
    if wind_ms < CUT_IN_MS or wind_ms >= CUT_OUT_MS:
        return 0.0
    if wind_ms >= RATED_MS:
        return 1.0
    return ((wind_ms - CUT_IN_MS) / (RATED_MS - CUT_IN_MS)) ** 3


def _wind_profile(issue_date: date, salt: str = "", bias: float = 0.0) -> list[float]:
    """Ветер по часам горизонта: суточный ход плюс блуждание."""
    rng = _rng(issue_date, salt)
    base = rng.uniform(4.0, 11.0) + bias
    profile: list[float] = []
    drift = 0.0
    for hour in range(HORIZON_HOURS):
        drift += rng.uniform(-0.45, 0.45)
        drift = max(-3.0, min(3.0, drift))
        diurnal = 1.4 * math.sin((hour / 24.0) * 2 * math.pi - 1.0)
        profile.append(round(max(0.0, base + drift + diurnal), 2))
    return profile


def _temperature_profile(issue_date: date) -> list[float]:
    rng = _rng(issue_date, "temp")
    base = rng.uniform(-14.0, 2.0)
    return [round(base + 3.0 * math.sin((hour / 24.0) * 2 * math.pi - 2.0), 1) for hour in range(HORIZON_HOURS)]


def _source_of(issue_date: date) -> tuple[str, bool]:
    """Каждый пятый день симуляции идет на запасном источнике."""
    degraded = issue_date.day % 5 == 0
    return (SOURCE_GFS if degraded else SOURCE_IFS, degraded)


def _flags_for(wind_ms: float, temp_c: float, ramp: float, degraded: bool) -> list[str]:
    flags: list[str] = []
    if wind_ms > CUT_OUT_MS:
        flags.append("cut_out_risk")
    if temp_c <= 1.0 and wind_ms > 2.0:
        flags.append("icing_risk")
    if abs(ramp) > 0.40:
        flags.append("ramp")
    if degraded:
        flags.append("degraded")
    return flags


def build_issue_summary(issue_date: date) -> IssueSummary:
    source, degraded = _source_of(issue_date)
    forecast = build_forecast(issue_date)
    return IssueSummary(
        issue_date=issue_date,
        issue_time_utc=issue_time(issue_date),
        target_date=issue_date + timedelta(days=1),
        version=2 if issue_date.day % 3 == 0 else 1,
        status="ready",
        degraded=degraded,
        source=source,
        flagged_hours=sum(1 for hour in forecast.hours if hour.flags),
    )


def list_issues() -> list[IssueSummary]:
    return [build_issue_summary(day) for day in issue_dates()]


def build_forecast(issue_date: date) -> ForecastResponse:
    """Почасовой прогноз на 48 часов по каждой турбине."""
    ensure_known_issue(issue_date)
    start = issue_time(issue_date)
    source, degraded = _source_of(issue_date)
    winds = _wind_profile(issue_date)
    temps = _temperature_profile(issue_date)
    run_init = start - timedelta(hours=8)
    available_at = run_init + timedelta(hours=7, minutes=30)

    hours: list[ForecastHour] = []
    for turbine in TURBINES:
        previous: list[float] = []
        for index in range(HORIZON_HOURS):
            valid = start + timedelta(hours=index + 1)
            # Турбины стоят в 340 м друг от друга, поэтому ветер почти одинаков.
            wind = round(winds[index] * (1.0 if turbine.name == "T1" else 0.98), 2)
            p50 = power_curve(wind)
            spread = 0.05 + 0.0035 * index + (0.03 if degraded else 0.0)
            ramp = p50 - previous[-3] if len(previous) >= 3 else 0.0
            hours.append(
                ForecastHour(
                    valid_time_utc=valid,
                    valid_time_local=valid + LOCAL_OFFSET,
                    lead_h=index + 1,
                    turbine=turbine.name,
                    p10=round(max(0.0, p50 - spread), 4),
                    p50=round(p50, 4),
                    p90=round(min(1.0, p50 + spread), 4),
                    p50_mw=round(p50 * turbine.rated_mw, 4),
                    wind_ms=wind,
                    temp_c=temps[index],
                    # Факта за февраль у нас нет, его держат организаторы.
                    actual=None,
                    source=source,
                    run_init_utc=run_init,
                    available_at_utc=available_at,
                    flags=_flags_for(wind, temps[index], ramp, degraded),
                )
            )
            previous.append(p50)

    day_hours = [hour for hour in hours if hour.valid_time_local.date() == issue_date + timedelta(days=1)]
    peak = max(day_hours, key=lambda hour: hour.p50)
    mean_load = sum(hour.p50 for hour in day_hours) / len(day_hours)
    return ForecastResponse(
        issue=IssueSummary(
            issue_date=issue_date,
            issue_time_utc=start,
            target_date=issue_date + timedelta(days=1),
            version=2 if issue_date.day % 3 == 0 else 1,
            status="ready",
            degraded=degraded,
            source=source,
            flagged_hours=sum(1 for hour in hours if hour.flags),
        ),
        kpi=ForecastKpi(
            mean_load_pct=round(mean_load * 100, 1),
            peak_hour_local=peak.valid_time_local,
            peak_mw=round(peak.p50_mw * len(TURBINES), 2),
            hours_below_10_pct=sum(1 for hour in day_hours if hour.p50 < 0.1) // len(TURBINES),
            expected_error_pct=round(11.0 + 4.0 * (1 if degraded else 0), 1),
            day_energy_mwh=round(sum(hour.p50_mw for hour in day_hours), 2),
        ),
        summary=(
            f"На сутки {(issue_date + timedelta(days=1)).isoformat()} ожидается средняя загрузка "
            f"{round(mean_load * 100)}%, пик в {peak.valid_time_local.strftime('%H:%M')} по местному времени. "
            + ("Основной прогон погоды не пришел, использован запасной источник." if degraded else "Прогноз построен на основном прогоне ECMWF.")
        ),
        hours=hours,
        data_source=DATA_SOURCE_STUB,
    )


def build_agent_log(issue_date: date) -> list[AgentDecision]:
    """Журнал решений агента по шести шагам ТЗ."""
    ensure_known_issue(issue_date)
    start = issue_time(issue_date)
    source, degraded = _source_of(issue_date)
    version = 2 if issue_date.day % 3 == 0 else 1

    log: list[AgentDecision] = []
    if degraded:
        log.append(
            AgentDecision(
                as_of_utc=start,
                step="fetch_weather",
                decision="try_next_source",
                reason_code="FALLBACK",
                reason=f"{SOURCE_IFS} недоступен на момент выпуска, беру запасной источник",
                level="WARN",
            )
        )
    log += [
        AgentDecision(as_of_utc=start, step="fetch_weather", decision="use_source", reason_code="USE_SOURCE", reason=f"взят {source}", level="TOOL"),
        AgentDecision(
            as_of_utc=start,
            step="prepare",
            decision="build_features",
            reason_code="USE_SOURCE",
            reason="признаки построены по прогнозу погоды",
            level="TOOL",
        ),
        AgentDecision(
            as_of_utc=start,
            step="run_model",
            decision="predict",
            reason_code="USE_SOURCE",
            reason="модель посчитала квантили P10/P50/P90",
            level="TOOL",
        ),
        AgentDecision(
            as_of_utc=start,
            step="forecast",
            decision="publish_version",
            reason_code="USE_SOURCE",
            reason=f"версия 1: {HORIZON_HOURS * len(TURBINES)} строк на горизонт +1…+{HORIZON_HOURS} ч",
            level="OK",
        ),
        AgentDecision(
            as_of_utc=start,
            step="analyze",
            decision="flag_hours",
            reason_code="RISK_FLAGS",
            reason="проверены диапазон, рампы, отсечка и обледенение",
            level="THINK",
        ),
    ]
    if version > 1:
        moment = start + timedelta(hours=5, minutes=30)
        log.append(
            AgentDecision(
                as_of_utc=moment,
                step="recompute_on_update",
                decision="publish_new_version",
                reason_code="PUBLISH_NEW_VERSION",
                reason="вышел новый прогон, выработка суток D изменилась больше чем на 3%",
                level="OK",
            )
        )
    else:
        log.append(
            AgentDecision(
                as_of_utc=start + timedelta(hours=5, minutes=30),
                step="recompute_on_update",
                decision="keep_version",
                reason_code="NO_MATERIAL_CHANGE",
                reason="новый прогон меняет сутки D меньше чем на 3%, версия оставлена прежней",
                level="OK",
            )
        )
    return log


def build_weather(issue_date: date) -> WeatherResponse:
    """Доступность прогонов и сравнение моделей погоды."""
    ensure_known_issue(issue_date)
    start = issue_time(issue_date)
    _, degraded = _source_of(issue_date)

    runs = [
        WeatherRun(
            source=SOURCE_IFS,
            run_init_utc=start - timedelta(hours=8),
            available_at_utc=start - timedelta(minutes=30),
            status="after_issue" if degraded else "used",
            lead_from_h=9,
            lead_to_h=56,
        ),
        WeatherRun(
            source=SOURCE_GFS,
            run_init_utc=start - timedelta(hours=8),
            available_at_utc=start - timedelta(hours=1),
            status="used" if degraded else "stale",
            lead_from_h=9,
            lead_to_h=56,
        ),
        WeatherRun(
            source=SOURCE_IFS,
            run_init_utc=start - timedelta(hours=2),
            available_at_utc=start + timedelta(hours=5, minutes=30),
            status="after_issue",
            lead_from_h=3,
            lead_to_h=50,
        ),
    ]

    weights = (0.38, 0.24, 0.22, 0.16)
    models: list[WeatherModel] = []
    for index, name in enumerate(WEATHER_MODELS):
        rng = _rng(issue_date, name)
        models.append(
            WeatherModel(
                name=name,
                weight=weights[index],
                wind_mae_ms=round(rng.uniform(1.1, 2.4), 2),
                wind_ms=_wind_profile(issue_date, salt=name, bias=rng.uniform(-1.2, 1.2)),
            )
        )

    ensemble = [round(sum(model.wind_ms[hour] * model.weight for model in models), 2) for hour in range(HORIZON_HOURS)]
    spread = sum(max(model.wind_ms[hour] for model in models) - min(model.wind_ms[hour] for model in models) for hour in range(HORIZON_HOURS))
    return WeatherResponse(
        issue_time_utc=start,
        runs=runs,
        models=models,
        ensemble_wind_ms=ensemble,
        spread_ms=round(spread / HORIZON_HOURS, 2),
        data_source=DATA_SOURCE_STUB,
    )


def build_dispatch(issue_date: date, risk: float) -> DispatchResponse:
    """Почасовая заявка: чем выше допустимый риск, тем ближе к P50."""
    ensure_known_issue(issue_date)
    forecast = build_forecast(issue_date)
    target = issue_date + timedelta(days=1)

    by_hour: dict[datetime, list[ForecastHour]] = {}
    for hour in forecast.hours:
        if hour.valid_time_local.date() == target:
            by_hour.setdefault(hour.valid_time_local, []).append(hour)

    hours: list[DispatchHour] = []
    for local_time in sorted(by_hour):
        rows = by_hour[local_time]
        p10 = sum(row.p10 * TURBINES[0].rated_mw for row in rows)
        p50 = sum(row.p50 * TURBINES[0].rated_mw for row in rows)
        p90 = sum(row.p90 * TURBINES[0].rated_mw for row in rows)
        bid = p10 + (risk - 0.1) / 0.4 * (p50 - p10)
        hours.append(
            DispatchHour(
                valid_time_local=local_time,
                bid_mw=round(bid, 3),
                p10_mw=round(p10, 3),
                p50_mw=round(p50, 3),
                p90_mw=round(p90, 3),
            )
        )

    day_bid = sum(hour.bid_mw for hour in hours)
    expected = sum(hour.p50_mw for hour in hours)
    return DispatchResponse(
        issue_date=issue_date,
        risk=risk,
        kpi=DispatchKpi(
            day_bid_mwh=round(day_bid, 2),
            expected_mwh=round(expected, 2),
            shortfall_hours_share=round(max(0.0, 0.5 - risk), 3),
            mean_shortfall_mwh=round(max(0.0, expected - day_bid) / max(1, len(hours)), 3),
        ),
        hours=hours,
        data_source=DATA_SOURCE_STUB,
    )


def build_backtest() -> BacktestMetrics:
    """Оценка качества. Позже приходит из reports/metrics.json от dev2."""
    days = issue_dates()
    by_day = []
    for day in days:
        rng = _rng(day, "backtest")
        by_day.append(BacktestDay(issue_date=day, nmae_pct=round(rng.uniform(8.0, 21.0), 1), bias_pct=round(rng.uniform(-4.0, 4.0), 1)))

    by_lead = [BacktestLead(lead_h=lead, nmae_pct=round(9.0 + 0.22 * lead, 1)) for lead in range(1, HORIZON_HOURS + 1)]
    return BacktestMetrics(
        period_start=days[0],
        period_end=days[-1],
        nmae_d1_pct=12.4,
        nmae_d2_pct=18.9,
        nrmse_48_pct=21.3,
        skill_vs_persistence_pct=58.0,
        coverage_p10_p90_pct=79.0,
        baselines=[
            BacktestBaseline(name="Персистентность", nmae_pct=31.2),
            BacktestBaseline(name="Климатология час × месяц", nmae_pct=26.7),
            BacktestBaseline(name="Паспортная кривая по ECMWF", nmae_pct=17.5),
        ],
        by_day=by_day,
        by_lead=by_lead,
        data_source=DATA_SOURCE_STUB,
    )


def build_model_info() -> ModelInfo:
    curve = [PowerCurvePoint(wind_ms=float(step) / 2, power_norm=round(power_curve(step / 2), 4)) for step in range(0, 53)]
    return ModelInfo(
        name="HistGradientBoostingRegressor, квантильная",
        quantiles=[0.1, 0.5, 0.9],
        trained_until=date(2026, 1, 31),
        train_rows=0,
        features=[
            FeatureImportance(name="ws100", importance=0.41),
            FeatureImportance(name="ws80", importance=0.17),
            FeatureImportance(name="air_density", importance=0.11),
            FeatureImportance(name="wd100_sin", importance=0.08),
            FeatureImportance(name="wd100_cos", importance=0.07),
            FeatureImportance(name="lead_h", importance=0.06),
            FeatureImportance(name="t2m", importance=0.05),
            FeatureImportance(name="gust10", importance=0.05),
        ],
        power_curve=curve,
        walk_forward="обучение до 31.01.2026, оценка на январе 2026, тест на феврале 2026",
        data_source=DATA_SOURCE_STUB,
    )


def build_site() -> SiteInfo:
    return SiteInfo(
        name="ВЭС «Шелек», поселок Нурлы",
        turbines=list(TURBINES),
        capacity_mw=sum(turbine.rated_mw for turbine in TURBINES),
        hub_height_m=HUB_HEIGHT_M,
        nwp_point_lat=NWP_POINT[0],
        nwp_point_lon=NWP_POINT[1],
        local_utc_offset_hours=int(LOCAL_OFFSET.total_seconds() // 3600),
        cut_in_ms=CUT_IN_MS,
        rated_ms=RATED_MS,
        cut_out_ms=CUT_OUT_MS,
    )
