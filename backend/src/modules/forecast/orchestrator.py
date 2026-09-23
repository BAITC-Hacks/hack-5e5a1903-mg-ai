"""Цикл агента поверх соседних модулей.

Шаги названы словами ТЗ: ``fetch_weather`` → ``prepare`` → ``run_model`` →
``forecast`` → ``analyze`` → ``recompute_on_update``. Это не один запрос
к модели, а детерминированная последовательность:

1. Агент спрашивает у ML-сервиса паспорт модели и берет источники погоды,
   на которых она обучена. Паспорт недоступен — работает список по умолчанию.
2. Берет у источника погоды (dev3) прогоны, доступные на момент выпуска,
   отбраковывает негодные, недостающие помечает как деградацию.
3. Считает расхождение источников и, если они разошлись, просит модель
   расширить интервал P10…P90.
4. Отдает строки «час × модель погоды» ML-сервису (dev2) и получает P10/P50/P90.
5. Собирает выпуск, проверяет его на риски и решает, публиковать ли новую
   версию после выхода нового прогона.

Каждое решение, которое меняет ход выполнения, попадает в журнал с кодом
причины: предметным (``USE_SOURCE``, ``FALLBACK``, ``VALIDATE_FAIL``,
``NO_SOURCE``, ``RISK_FLAGS``, ``LEAKAGE_DROPPED``, ``PUBLISH_NEW_VERSION``,
``NO_MATERIAL_CHANGE``), кодом отказа соседа (``WEATHER_NO_RUN``,
``ML_TIMEOUT``, ``ML_UNAVAILABLE``) или кодом предупреждения модели
(``BASELINE_MODEL``, ``SOURCE_MISSING``, ``PARTIAL_SOURCE``).

**Единственная точка переключения live и stub — функция ``run``.** Пока сосед
не отвечает, эндпоинты продолжают отдавать выпуск из ``service.py``
с ``data_source="stub"``; как только данные реально пришли, ``data_source``
становится ``live``. Нигде больше этой развилки нет.

Исключение одно: утечка будущего. Погода, опубликованная позже момента выпуска,
не заменяется демонстрацией, а честно превращается в ошибку ``WEATHER_LEAKAGE``:
спрятать утечку за заглушкой хуже, чем показать отказ.

Состояния в памяти процесса нет: журнал, клиенты и хранилище погоды создаются
на каждый запрос.
"""

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from src.core.exceptions import BusinessError
from src.modules.forecast import analyze, service
from src.modules.forecast.clients.base import UpstreamError, iso_utc
from src.modules.forecast.clients.ml import MlClient
from src.modules.forecast.clients.schemas import ForecastPoint, NwpRow, PredictResponse, RunRow
from src.modules.forecast.clients.weather import CODE_NO_RUN, WeatherGateway, WeatherSource
from src.modules.forecast.config import DEFAULT_SOURCES, HORIZON_HOURS, LOCAL_OFFSET, RUN_CYCLE, SOURCE_ENSEMBLE, TURBINES, rated_mw
from src.modules.forecast.decisions import (
    LEVEL_OK,
    LEVEL_THINK,
    LEVEL_WARN,
    REASON_FALLBACK,
    REASON_LEAKAGE_DROPPED,
    REASON_NO_MATERIAL_CHANGE,
    REASON_NO_SOURCE,
    REASON_PUBLISH_NEW_VERSION,
    REASON_RISK_FLAGS,
    REASON_USE_SOURCE,
    REASON_VALIDATE_FAIL,
    STEP_ANALYZE,
    STEP_FETCH_WEATHER,
    STEP_FORECAST,
    STEP_PREPARE,
    STEP_RECOMPUTE,
    STEP_RUN_MODEL,
    DecisionLog,
)
from src.modules.forecast.schemas import (
    AgentDecision,
    BacktestBaseline,
    BacktestDay,
    BacktestLead,
    BacktestMetrics,
    DispatchResponse,
    FeatureImportance,
    ForecastHour,
    ForecastResponse,
    IssueSummary,
    ModelInfo,
    PowerCurvePoint,
)

logger = logging.getLogger(__name__)

DATA_SOURCE_LIVE = "live"

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_RECOMPUTE = "recompute_on_update"

#: Во сколько раз просим модель расширить интервал, когда источники разошлись.
WIDE_INTERVAL_SCALE = 1.5
#: Сколько времени после выпуска агент реагирует на новые прогоны.
RECOMPUTE_WINDOW = timedelta(days=1)
#: Турбины, на которые агент просит прогноз у модели.
TURBINE_NAMES = [turbine.name for turbine in TURBINES]
#: Код ML-сервиса «модель еще не оценена». Показываем его как есть.
UPSTREAM_METRICS_NOT_AVAILABLE = "METRICS_NOT_AVAILABLE"


def weather_source() -> WeatherSource:
    """Источник погоды: сервис dev3, а если он молчит — кэш прогнозов из образа.

    Отдельная функция, чтобы тест мог подставить свою погоду и не поднимать
    ни сервис, ни файлы.
    """
    return WeatherGateway()


def ml_client() -> MlClient:
    """Клиент ML-сервиса. Отдельная функция, чтобы тест мог подменить транспорт."""
    return MlClient()


@dataclass(frozen=True)
class AgentRun:
    """Результат цикла: выпуск и журнал решений, которые к нему привели."""

    forecast: ForecastResponse
    decisions: list[AgentDecision]


@dataclass
class _Weather:
    """Погода выпуска: строки «час × модель», ансамбль и что с ним не так."""

    rows: list[NwpRow]
    points: dict[datetime, analyze.WeatherPoint]
    sources: list[str]
    spread_ms: float | None = None
    flags: set[str] = field(default_factory=set)

    @property
    def label(self) -> str:
        return "+".join(self.sources)


@dataclass
class _Pass:
    """Один проход цикла от погоды до анализа."""

    hours: list[ForecastHour]
    weather: _Weather
    model_name: str
    flags: set[str] = field(default_factory=set)

    @property
    def degraded(self) -> bool:
        return analyze.FLAG_DEGRADED in self.flags


def horizon(issue_time: datetime) -> list[datetime]:
    """Часы прогноза: +1…+48 от момента выпуска."""
    return [issue_time + timedelta(hours=lead) for lead in range(1, HORIZON_HOURS + 1)]


def newest_rows(rows: list[NwpRow]) -> dict[datetime, NwpRow]:
    """По одной строке на час: из нескольких источников берется свежий прогон.

    Из нее в ответ уходят ``run_init_utc`` и ``available_at_utc``, то есть
    ответ на вопрос «откуда этот час и когда он стал известен».
    """
    newest: dict[datetime, NwpRow] = {}
    for row in rows:
        current = newest.get(row.valid_time_utc)
        if current is None or row.run_init_utc > current.run_init_utc:
            newest[row.valid_time_utc] = row
    return newest


async def weather_for_issue(issue_time: datetime, journal: DecisionLog) -> "_Weather":
    """Погода на момент выпуска отдельно от цикла модели.

    Нужна прогнозу по загруженному датасету: тот же ``WeatherGateway``, тот же
    ``as_of``, та же отбраковка негодных прогонов. Источники берутся по умолчанию,
    потому что модель здесь не участвует: кривую мощности дают данные пользователя.
    """
    journal.at(STEP_FETCH_WEATHER)
    return await _fetch_weather(weather_source(), issue_time, issue_time, horizon(issue_time), list(DEFAULT_SOURCES), journal)


async def run(issue_date: date, *, trigger: str = TRIGGER_SCHEDULED) -> AgentRun:
    """Выпуск на указанный день.

    Единственное место, где решается, живой выпуск или демонстрационный.
    Отказ соседа не роняет запрос и не отдает пустой экран: он записывается
    в журнал с кодом причины, а выпуск собирается заглушкой. Все остальные
    ошибки, например неизвестная дата или утечка будущего, так и уходят клиенту.
    """
    service.ensure_known_issue(issue_date)
    issue_time = service.issue_time(issue_date)
    journal = DecisionLog(issue_time_utc=issue_time)

    try:
        return await _run_live(issue_date, issue_time, trigger, journal)
    except UpstreamError as error:
        logger.warning("Выпуск за %s собран заглушкой: %s на шаге %s", issue_date, error.code, journal.step)
        journal.record(
            "fall_back_to_stub",
            error.code,
            f"{error.message}. Выпуск собран демонстрационными данными, числа синтетические",
            level=LEVEL_WARN,
        )
        return AgentRun(forecast=service.build_forecast(issue_date), decisions=journal.rows + service.build_agent_log(issue_date))


async def dispatch(issue_date: date, risk: float) -> DispatchResponse:
    """Заявка диспетчера на сутки D по тому же выпуску, что видит «Обзор»."""
    agent_run = await run(issue_date)
    return service.dispatch_from_forecast(agent_run.forecast, risk)


async def model_info() -> ModelInfo:
    """Паспорт модели со страницы «Модель».

    Живой паспорт показывается, когда модель обучена. Пока ML-сервис работает
    на паспортной кривой, у него нет ни даты обучения, ни размера выборки,
    а выдумывать их нельзя: страница остается демонстрационной и честно
    помечена ``stub``.
    """
    try:
        info = await ml_client().model_info()
    except UpstreamError as error:
        logger.warning("Паспорт модели собран заглушкой: %s", error.code)
        return service.build_model_info()

    if info.trained_until_utc is None:
        logger.info("Модель %s не обучена (%s), паспорт остается демонстрационным", info.name, info.kind)
        return service.build_model_info()

    return ModelInfo(
        name=f"{info.name} {info.version}",
        quantiles=info.quantiles,
        trained_until=info.trained_until_utc.date(),
        train_rows=info.train_rows or 0,
        features=[FeatureImportance(name=item.name, importance=item.importance) for item in info.features],
        power_curve=[PowerCurvePoint(wind_ms=point.wind_ms, power_norm=point.power_norm) for point in info.power_curve],
        walk_forward=info.walk_forward or "",
        data_source=DATA_SOURCE_LIVE,
    )


async def backtest() -> BacktestMetrics:
    """Метрики со страницы «Бэктест».

    Модель еще не оценена — это не повод показывать синтетику под видом оценки:
    ML-сервис отвечает ``METRICS_NOT_AVAILABLE``, и то же самое видит пользователь.
    Сервис недоступен целиком — страница работает на демонстрационных числах.
    """
    try:
        metrics = await ml_client().metrics()
    except UpstreamError as error:
        if error.details.get("upstream_code") == UPSTREAM_METRICS_NOT_AVAILABLE:
            raise BusinessError(
                404,
                UPSTREAM_METRICS_NOT_AVAILABLE,
                "Метрик пока нет: модель еще не оценена на отложенном периоде",
            ) from error
        logger.warning("Метрики собраны заглушкой: %s", error.code)
        return service.build_backtest()

    return BacktestMetrics(
        period_start=metrics.period_start_utc.date(),
        period_end=metrics.period_end_utc.date(),
        nmae_d1_pct=metrics.nmae_d1_pct,
        nmae_d2_pct=metrics.nmae_d2_pct,
        nrmse_48_pct=metrics.nrmse_48_pct,
        skill_vs_persistence_pct=metrics.skill_vs_persistence_pct,
        coverage_p10_p90_pct=metrics.coverage_p10_p90_pct,
        baselines=[BacktestBaseline(name=item.name, nmae_pct=item.nmae_pct) for item in metrics.baselines],
        by_day=[BacktestDay(issue_date=day.issue_time_utc.date(), nmae_pct=day.nmae_pct, bias_pct=day.bias_pct) for day in metrics.by_day],
        by_lead=[BacktestLead(lead_h=lead.lead_h, nmae_pct=lead.nmae_pct) for lead in metrics.by_lead],
        data_source=DATA_SOURCE_LIVE,
    )


async def _run_live(issue_date: date, issue_time: datetime, trigger: str, journal: DecisionLog) -> AgentRun:
    """Живой цикл. Любой отказ соседа улетает наверх как ``UpstreamError``."""
    weather = weather_source()
    model = ml_client()

    published = await _single_pass(weather, model, issue_time, issue_time, journal)
    version, published = await _recompute_on_update(weather, model, issue_time, trigger, published, journal)
    return AgentRun(forecast=_assemble_response(issue_date, issue_time, version, published), decisions=journal.rows)


async def _single_pass(weather: WeatherSource, model: MlClient, issue_time: datetime, as_of: datetime, journal: DecisionLog) -> _Pass:
    """fetch_weather → prepare → run_model → forecast → analyze."""
    expected = horizon(issue_time)

    journal.at(STEP_FETCH_WEATHER)
    sources = await _model_sources(model, journal, as_of)
    collected = await _fetch_weather(weather, issue_time, as_of, expected, sources, journal)

    journal.at(STEP_PREPARE)
    journal.record(
        "build_features",
        REASON_USE_SOURCE,
        f"модели уходят {len(collected.rows)} строк «час × модель погоды» от источников {collected.label}",
        as_of=as_of,
    )

    prediction = await _run_model(model, issue_time, as_of, collected, journal)

    journal.at(STEP_FORECAST)
    hours = _assemble_hours(issue_time, collected, prediction)
    journal.record(
        "publish_version",
        REASON_USE_SOURCE,
        f"{len(hours)} строк на горизонт +1…+{HORIZON_HOURS} ч по {len(TURBINE_NAMES)} турбинам, модель {prediction.model.name}",
        level=LEVEL_OK,
        as_of=as_of,
    )

    result = _Pass(hours=hours, weather=collected, model_name=prediction.model.name, flags=set(collected.flags))
    if prediction.degraded:
        result.flags.add(analyze.FLAG_DEGRADED)
    _analyze(result, journal, as_of)
    return result


async def _model_sources(model: MlClient, journal: DecisionLog, as_of: datetime) -> list[str]:
    """Источники погоды, на которых обучена модель.

    Спрашиваем у самой модели: обучение и прогноз должны видеть одни и те же
    модели погоды. Паспорт недоступен или не называет источники — берем список
    по умолчанию, это записывается в журнал.
    """
    try:
        info = await model.model_info()
    except UpstreamError as error:
        journal.record(
            "use_default_sources",
            error.code,
            f"паспорт модели недоступен ({error.message}), беру источники по умолчанию: {', '.join(DEFAULT_SOURCES)}",
            level=LEVEL_WARN,
            as_of=as_of,
        )
        return list(DEFAULT_SOURCES)

    names = [source.name for source in info.inputs.sources]
    if not names:
        journal.record(
            "use_default_sources",
            REASON_USE_SOURCE,
            f"модель {info.name} не привязана к источникам, беру список по умолчанию: {', '.join(DEFAULT_SOURCES)}",
            as_of=as_of,
        )
        return list(DEFAULT_SOURCES)

    journal.record(
        "use_model_sources",
        REASON_USE_SOURCE,
        f"модель {info.name} обучена на источниках {', '.join(names)}, беру их",
        as_of=as_of,
    )
    return names


async def _fetch_weather(
    weather: WeatherSource,
    issue_time: datetime,
    as_of: datetime,
    expected: list[datetime],
    sources: list[str],
    journal: DecisionLog,
) -> _Weather:
    """Шаг 1: погода, доступная на момент ``as_of``, по каждому нужному источнику.

    Источник без прогона или с негодным прогоном пропускается, выпуск помечается
    как построенный не в полной конфигурации. Забракованный прогон агент пробует
    переспросить на цикл назад: возможно, испорчен только самый свежий.
    Не подошло ничего — выпуск уходит на заглушку, климатологии у бэкенда нет,
    она живет в офлайн-пайплайне.
    """
    rows: list[NwpRow] = []
    points: dict[str, dict[datetime, analyze.WeatherPoint]] = {}
    used: list[str] = []
    attempts: list[str] = []
    sources = _without_double_counting(sources, journal, as_of)

    for source in sources:
        journal.record("call_weather_source", REASON_USE_SOURCE, f"запрашиваю {source} на момент {iso_utc(as_of)}", as_of=as_of)
        picked = await _usable_run(weather, source, issue_time, as_of, expected, journal, attempts)
        _record_source_switch(weather, journal, as_of)
        if picked is None:
            continue
        rows.extend(picked)
        points[source] = _weather_points(picked)
        used.append(source)

    if not used:
        journal.record("give_up_sources", REASON_NO_SOURCE, "ни один прогон не подошел: " + "; ".join(attempts), level=LEVEL_WARN, as_of=as_of)
        raise UpstreamError(
            status_code=503,
            code=CODE_NO_RUN,
            message="Ни один источник погоды не отдал годного прогона на момент выпуска",
            details={"attempts": attempts, "sources": sources},
        )

    collected = _Weather(rows=rows, points=analyze.ensemble_points(points), sources=used)
    if len(used) < len(sources):
        collected.flags.add(analyze.FLAG_DEGRADED)
        journal.record(
            "mark_degraded",
            REASON_FALLBACK,
            f"выпуск построен без источников {', '.join(name for name in sources if name not in used)}",
            level=LEVEL_WARN,
            as_of=as_of,
        )

    collected.spread_ms = analyze.source_spread(*points.values())
    if collected.spread_ms is not None and collected.spread_ms > analyze.SOURCE_SPREAD_MS:
        collected.flags.add(analyze.FLAG_SOURCE_SPREAD)
        journal.record(
            "mark_source_spread",
            REASON_VALIDATE_FAIL,
            f"источники расходятся по ветру на {collected.spread_ms:.1f} м/с при пороге {analyze.SOURCE_SPREAD_MS:.0f}",
            level=LEVEL_WARN,
            as_of=as_of,
        )
    else:
        journal.record(
            "use_sources",
            REASON_USE_SOURCE,
            f"взяты {collected.label}, часов {len(collected.points)}"
            + (f", расхождение по ветру {collected.spread_ms:.1f} м/с" if collected.spread_ms is not None else ""),
            level=LEVEL_OK,
            as_of=as_of,
        )
    return collected


def _without_double_counting(sources: list[str], journal: DecisionLog, as_of: datetime) -> list[str]:
    """Готовый ансамбль и его участники вместе не отправляются.

    Сервис погоды умеет отдавать источник ``ensemble``, а ML-сервис усредняет
    модели сам. Прислать и то и другое значило бы посчитать одни и те же модели
    дважды, поэтому берется только ансамбль.
    """
    if SOURCE_ENSEMBLE not in sources or len(sources) == 1:
        return sources

    members = ", ".join(name for name in sources if name != SOURCE_ENSEMBLE)
    journal.record(
        "use_ensemble_only",
        REASON_USE_SOURCE,
        f"источник {SOURCE_ENSEMBLE} уже усредняет модели, участники {members} не запрашиваются",
        as_of=as_of,
    )
    return [SOURCE_ENSEMBLE]


def _record_source_switch(weather: WeatherSource, journal: DecisionLog, as_of: datetime) -> None:
    """Переход на запасной источник погоды не должен пройти молча."""
    drain = getattr(weather, "drain_notes", None)
    if drain is None:
        return
    for code, message in drain():
        journal.record("use_spare_weather", code, message, level=LEVEL_WARN, as_of=as_of)


async def _usable_run(
    weather: WeatherSource,
    source: str,
    issue_time: datetime,
    as_of: datetime,
    expected: list[datetime],
    journal: DecisionLog,
    attempts: list[str],
) -> list[NwpRow] | None:
    """Годный прогон источника или ``None``. Забракованный пробуем на цикл назад."""
    for cycles_back in (0, 1):
        moment = as_of - cycles_back * RUN_CYCLE
        try:
            rows = await weather.nwp(source=source, as_of=moment, valid_times=expected)
        except UpstreamError as error:
            attempts.append(f"{source}: {error.code}")
            # «Прогона нет» это штатный сценарий и пишется как FALLBACK,
            # отказ сервиса пишется своим кодом: причины разные.
            reason_code = REASON_FALLBACK if error.code == CODE_NO_RUN else error.code
            journal.record("skip_source", reason_code, f"{source} не отдал прогон: {error.message}", level=LEVEL_WARN, as_of=as_of)
            return None

        usable = _drop_leakage(rows, issue_time, moment, journal, as_of)
        problems = analyze.validate_nwp(_weather_points(usable), expected)
        if not problems:
            if cycles_back:
                journal.record(
                    "use_previous_run",
                    REASON_FALLBACK,
                    f"{source}: взят прогон на цикл назад, свежий забракован",
                    level=LEVEL_WARN,
                    as_of=as_of,
                )
            return usable

        attempts.append(f"{source}: {'; '.join(problems)}")
        journal.record("reject_run", REASON_VALIDATE_FAIL, f"{source}: {'; '.join(problems)}", level=LEVEL_WARN, as_of=as_of)
    return None


def _drop_leakage(rows: list[NwpRow], issue_time: datetime, as_of: datetime, journal: DecisionLog, log_as_of: datetime) -> list[NwpRow]:
    """Оставить только то, что опубликовано к ``as_of`` и лежит на горизонте.

    Источник погоды фильтрует сам, но утечка будущего дороже лишней проверки,
    поэтому агент перепроверяет и пишет в журнал, что выбросил.
    """
    kept: list[NwpRow] = []
    leaked = 0
    for row in rows:
        if row.available_at_utc > as_of:
            leaked += 1
            continue
        lead = (row.valid_time_utc - issue_time).total_seconds() / 3600
        if lead.is_integer() and 1 <= lead <= HORIZON_HOURS:
            kept.append(row)

    if leaked:
        journal.record(
            "drop_rows",
            REASON_LEAKAGE_DROPPED,
            f"выброшены {leaked} строк погоды, опубликованных позже {iso_utc(as_of)}: это утечка будущего",
            level=LEVEL_WARN,
            as_of=log_as_of,
        )
    return kept


async def _run_model(model: MlClient, issue_time: datetime, as_of: datetime, collected: _Weather, journal: DecisionLog) -> PredictResponse:
    """Шаг 3: квантили считает модель dev2, бэкенд их только проверяет."""
    journal.at(STEP_RUN_MODEL)
    scale = WIDE_INTERVAL_SCALE if analyze.FLAG_SOURCE_SPREAD in collected.flags else 1.0
    journal.record(
        "call_model_service",
        REASON_USE_SOURCE,
        f"отдаю модели {len(collected.rows)} строк погоды"
        + (f", интервал P10…P90 расширен в {scale} раза: источники разошлись" if scale > 1 else ""),
        as_of=as_of,
    )

    prediction = await model.predict(
        issue_time_utc=issue_time,
        rows=collected.rows,
        turbines=TURBINE_NAMES,
        interval_scale=scale,
        request_id=f"issue-{iso_utc(issue_time)}-asof-{iso_utc(as_of)}",
    )

    for warning in prediction.warnings:
        journal.record("note_model_warning", warning.code, warning.message, level=LEVEL_WARN, as_of=as_of)

    indexed = _index_predictions(prediction.forecast, collected)
    journal.record(
        "predict",
        REASON_USE_SOURCE,
        f"модель {prediction.model.name} ({prediction.model.version}) вернула P10/P50/P90 на {len(indexed) // len(TURBINE_NAMES)} часов",
        level=LEVEL_OK,
        as_of=as_of,
    )
    return prediction


def _index_predictions(points: list[ForecastPoint], collected: _Weather) -> dict[tuple[str, datetime], ForecastPoint]:
    """Квантили по ключу «турбина, час». Неполный ответ модели это отказ, а не дыра в прогнозе."""
    indexed = {(point.turbine, point.valid_time_utc): point for point in points}
    missing = [(name, moment) for name in TURBINE_NAMES for moment in collected.points if (name, moment) not in indexed]
    if missing:
        raise UpstreamError(
            status_code=502,
            code="ML_BAD_RESPONSE",
            message=f"Сервис модели вернул квантили не на все часы: не хватает {len(missing)} из {len(collected.points) * len(TURBINE_NAMES)}",
            details={"missing": len(missing), "first_missing": f"{missing[0][0]} {iso_utc(missing[0][1])}"},
        )
    return indexed


def _weather_points(rows: list[NwpRow]) -> dict[datetime, analyze.WeatherPoint]:
    """Погода одного источника в том виде, в каком ее смотрят проверки."""
    return {
        row.valid_time_utc: analyze.WeatherPoint(wind_ms=row.hub_wind_ms, temp_c=row.t2m, humidity_pct=row.rh2m)
        for row in sorted(rows, key=lambda row: row.valid_time_utc)
    }


def _assemble_hours(issue_time: datetime, collected: _Weather, prediction: PredictResponse) -> list[ForecastHour]:
    """Шаг 4: контракт фронтенда из ответов соседей. Ничего не досчитывается."""
    indexed = _index_predictions(prediction.forecast, collected)
    meta = newest_rows(collected.rows)

    hours: list[ForecastHour] = []
    for name in TURBINE_NAMES:
        capacity = prediction.capacity_mw.get(name, rated_mw(name))
        for moment in sorted(collected.points):
            point = collected.points[moment]
            quantiles = indexed[(name, moment)]
            row = meta[moment]
            hours.append(
                ForecastHour(
                    valid_time_utc=moment,
                    valid_time_local=moment + LOCAL_OFFSET,
                    lead_h=int((moment - issue_time).total_seconds() // 3600),
                    turbine=name,
                    p10=quantiles.p10,
                    p50=quantiles.p50,
                    p90=quantiles.p90,
                    p50_mw=round(quantiles.p50 * capacity, 4),
                    wind_ms=point.wind_ms,
                    temp_c=point.temp_c,
                    # Факта за февраль у нас нет, его держат организаторы.
                    actual=None,
                    source=collected.label,
                    run_init_utc=row.run_init_utc,
                    available_at_utc=row.available_at_utc,
                )
            )
    return hours


def _analyze(result: _Pass, journal: DecisionLog, as_of: datetime) -> None:
    """Шаг 5: самопроверка выпуска. Факта нет, поэтому агент проверяет себя."""
    journal.at(STEP_ANALYZE)
    for hour, flags in zip(result.hours, analyze.risk_flags(result.hours, result.weather.points, result.flags), strict=True):
        hour.flags = flags

    counts = analyze.flag_counts(result.hours)
    journal.record(
        "flag_hours" if counts else "no_findings",
        REASON_RISK_FLAGS,
        "проверены диапазон, рампы, отсечка и обледенение" + (f": {counts}" if counts else ": замечаний нет"),
        level=LEVEL_WARN if counts else LEVEL_THINK,
        as_of=as_of,
    )


async def _recompute_on_update(
    weather: WeatherSource,
    model: MlClient,
    issue_time: datetime,
    trigger: str,
    published: _Pass,
    journal: DecisionLog,
) -> tuple[int, _Pass]:
    """Шаг 6: новый прогон меняет выпуск, только если меняет его существенно."""
    journal.at(STEP_RECOMPUTE)
    target_date = (issue_time + LOCAL_OFFSET).date() + timedelta(days=1)
    pending = await _pending_runs(weather, issue_time, published.weather.sources, journal)

    if trigger != TRIGGER_RECOMPUTE:
        journal.record(
            "keep_version",
            REASON_NO_MATERIAL_CHANGE,
            f"версия 1 опубликована, новых прогонов после момента выпуска ожидается {len(pending)}",
            level=LEVEL_OK,
        )
        return 1, published

    if not pending:
        journal.record("keep_version", REASON_NO_MATERIAL_CHANGE, "новых прогонов после момента выпуска нет, пересчитывать нечего", level=LEVEL_OK)
        return 1, published

    event = pending[-1]
    moment = event.available_at_utc
    candidate = await _single_pass(weather, model, issue_time, moment, journal)

    journal.at(STEP_RECOMPUTE)
    changed, reason = analyze.material_change(published.hours, candidate.hours, target_date)
    decision = "publish_new_version" if changed else "keep_version"
    code = REASON_PUBLISH_NEW_VERSION if changed else REASON_NO_MATERIAL_CHANGE
    journal.record(decision, code, f"прогон {event.source} от {iso_utc(event.run_init_utc)}: {reason}", level=LEVEL_OK, as_of=moment)
    return (2, candidate) if changed else (1, published)


async def _pending_runs(weather: WeatherSource, issue_time: datetime, sources: list[str], journal: DecisionLog) -> list[RunRow]:
    """Прогоны, вышедшие после момента выпуска. Их отсутствие не повод ронять выпуск."""
    try:
        events = await weather.runs(time_from=issue_time, time_to=issue_time + RECOMPUTE_WINDOW, sources=sources)
    except UpstreamError as error:
        journal.record(
            "continue_without_run_events",
            error.code,
            f"список прогонов недоступен: {error.message}. Пересчет по новому прогону в этом выпуске невозможен",
            level=LEVEL_WARN,
        )
        return []
    return sorted(events, key=lambda event: event.available_at_utc)


def _assemble_response(issue_date: date, issue_time: datetime, version: int, result: _Pass) -> ForecastResponse:
    """Сборка ответа фронтенду. Формы полей те же, что у заглушки."""
    target_date = issue_date + timedelta(days=1)
    spread = [hour.p90 - hour.p10 for hour in result.hours]
    kpi = service.build_kpi(result.hours, target_date, sum(spread) / len(spread) / 2 * 100)
    counts = analyze.flag_counts(result.hours)
    risks = ", ".join(f"{flag}: {count} ч" for flag, count in counts.items())

    return ForecastResponse(
        issue=IssueSummary(
            issue_date=issue_date,
            issue_time_utc=issue_time,
            target_date=target_date,
            version=version,
            status="ready",
            degraded=result.degraded,
            source=result.weather.label,
            flagged_hours=sum(1 for hour in result.hours if hour.flags),
        ),
        kpi=kpi,
        summary=(
            f"На сутки {target_date.isoformat()} ожидается средняя загрузка {round(kpi.mean_load_pct)}%, "
            f"пик в {kpi.peak_hour_local.strftime('%H:%M')} по местному времени, выработка {kpi.day_energy_mwh} МВт·ч. "
            f"Прогноз посчитан моделью {result.model_name} по источникам погоды {result.weather.label}"
            + (", выпуск построен не в полной конфигурации. " if result.degraded else ". ")
            + (f"Отмечены риски: {risks}." if risks else "Рисков по часам не отмечено.")
        ),
        hours=result.hours,
        data_source=DATA_SOURCE_LIVE,
    )
