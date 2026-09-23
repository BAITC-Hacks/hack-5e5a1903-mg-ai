"""Агент выпуска прогноза: детерминированная state machine.

Шаги названы словами ТЗ: ``fetch_weather`` -> ``prepare`` -> ``run_model`` ->
``forecast`` -> ``analyze`` -> ``recompute_on_update``. Агент сам выбирает
источник погоды, отбраковывает негодный прогон, откатывается на запасной
источник и решает, публиковать ли новую версию после выхода нового прогона.
Каждое такое решение попадает в журнал вместе с причиной.

Политика по умолчанию детерминированная: ни сети, ни ключей, ни LLM.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from ..config import (
    HORIZON_HOURS,
    ISSUE_HOUR_UTC,
    LOCAL_UTC_OFFSET,
    RATED_MW_PER_TURBINE,
    RUN_CYCLE,
    SOURCE_GFS,
    SOURCE_IFS,
)
from ..contract import FORECAST_COLUMNS, NWP_COLUMNS, IssueResult, LeakageError, Model, NoRunAvailable
from .analyze import (
    FLAG_DEGRADED,
    FLAG_SOURCE_SPREAD,
    SOURCE_SPREAD_MS,
    material_change,
    risk_flags,
    source_spread,
    validate_nwp,
)
from .decisions import (
    REASON_FALLBACK,
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
from .providers import Providers

#: Источник, когда ни один прогон не подошел и прогноз строится по климатологии.
SOURCE_CLIMATOLOGY = "climatology"

#: Цепочка попыток: основной источник, запасной, затем те же на прогон назад.
FETCH_CHAIN: tuple[tuple[str, int], ...] = (
    (SOURCE_IFS, 0),
    (SOURCE_GFS, 0),
    (SOURCE_IFS, 1),
    (SOURCE_GFS, 1),
)

#: Сколько времени после выпуска агент реагирует на новые прогоны.
RECOMPUTE_WINDOW = pd.Timedelta(days=1)

IssueSink = Callable[[Sequence[IssueResult], Path], None]


def as_utc(value: datetime | pd.Timestamp | str) -> pd.Timestamp:
    """Любой момент времени к tz-aware UTC."""
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def horizon(issue_time: datetime | pd.Timestamp) -> pd.DatetimeIndex:
    """Часы прогноза: +1...+48 от момента выпуска."""
    start = as_utc(issue_time) + pd.Timedelta(hours=1)
    return pd.date_range(start, periods=HORIZON_HOURS, freq="h")


def issue_time_for(day: date | pd.Timestamp, issue_hour_utc: int = ISSUE_HOUR_UTC) -> pd.Timestamp:
    """Момент выпуска этого дня: 07:00 по Астане, то есть 02:00 UTC."""
    return as_utc(pd.Timestamp(pd.Timestamp(day).date())) + pd.Timedelta(hours=issue_hour_utc)


def _empty_nwp(valid_times: pd.DatetimeIndex) -> pd.DataFrame:
    """Пустая таблица погоды нужного формата: для климатологии и для паспорта."""
    frame = pd.DataFrame({column: pd.Series(dtype="float64") for column in NWP_COLUMNS})
    frame["valid_time_utc"] = pd.Series(valid_times, dtype="datetime64[ns, UTC]")
    return frame.reindex(columns=list(NWP_COLUMNS))


def _fetch_weather(
    as_of: pd.Timestamp,
    valid_times: pd.DatetimeIndex,
    providers: Providers,
    log: DecisionLog,
) -> tuple[pd.DataFrame | None, str, set[str]]:
    """Шаг 1: выбрать источник погоды, доступный на момент прогноза."""
    flags: set[str] = set()
    attempts: list[str] = []

    for position, (source, cycles_back) in enumerate(FETCH_CHAIN):
        attempt_as_of = as_of - cycles_back * RUN_CYCLE
        try:
            nwp = providers.get_nwp(source, attempt_as_of.to_pydatetime(), valid_times)
        except (NoRunAvailable, LeakageError) as error:
            attempts.append(f"{source}: {error.__class__.__name__}")
            log.record(
                STEP_FETCH_WEATHER,
                "try_next_source",
                REASON_FALLBACK,
                f"{source} недоступен: {error}",
                as_of=as_of,
                source=source,
                cycles_back=cycles_back,
            )
            continue

        problems = validate_nwp(nwp, valid_times)
        if problems:
            attempts.append(f"{source}: {'; '.join(problems)}")
            log.record(
                STEP_FETCH_WEATHER,
                "reject_run",
                REASON_VALIDATE_FAIL,
                "; ".join(problems),
                as_of=as_of,
                source=source,
                cycles_back=cycles_back,
            )
            continue

        if position > 0:
            flags.add(FLAG_DEGRADED)
        log.record(
            STEP_FETCH_WEATHER,
            "use_source",
            REASON_USE_SOURCE,
            f"взят {source}" + (f", прогон на {cycles_back} цикла назад" if cycles_back else ""),
            as_of=as_of,
            source=source,
            cycles_back=cycles_back,
            run_init_utc=nwp["run_init_utc"].max(),
            available_at_utc=nwp["available_at_utc"].max(),
            hours=int(len(nwp)),
            rejected=attempts,
        )
        flags |= _compare_sources(source, nwp, as_of, valid_times, providers, log)
        return nwp, source, flags

    log.record(
        STEP_FETCH_WEATHER,
        "use_climatology",
        REASON_NO_SOURCE,
        "ни один прогон не подошел, прогноз строится по климатологии",
        as_of=as_of,
        rejected=attempts,
    )
    flags.add(FLAG_DEGRADED)
    return None, SOURCE_CLIMATOLOGY, flags


def _compare_sources(
    primary: str,
    nwp: pd.DataFrame,
    as_of: pd.Timestamp,
    valid_times: pd.DatetimeIndex,
    providers: Providers,
    log: DecisionLog,
) -> set[str]:
    """Сверка с другим источником: сильное расхождение по ветру помечает прогноз."""
    other = SOURCE_GFS if primary == SOURCE_IFS else SOURCE_IFS
    try:
        alternative = providers.get_nwp(other, as_of.to_pydatetime(), valid_times)
    except (NoRunAvailable, LeakageError):
        return set()

    spread = source_spread(nwp, alternative)
    if spread is None or spread <= SOURCE_SPREAD_MS:
        return set()

    log.record(
        STEP_FETCH_WEATHER,
        "mark_source_spread",
        REASON_VALIDATE_FAIL,
        f"{primary} и {other} расходятся по ветру на {spread:.1f} м/с при пороге {SOURCE_SPREAD_MS:.0f}",
        as_of=as_of,
        spread_ms=round(spread, 2),
    )
    return {FLAG_SOURCE_SPREAD}


def _assemble_forecast(
    issue_time: pd.Timestamp,
    version: int,
    predictions: pd.DataFrame,
    nwp: pd.DataFrame | None,
    source: str,
) -> pd.DataFrame:
    """Шаг 4: привести прогноз к формату выходного файла."""
    frame = predictions.copy()
    frame["valid_time_utc"] = pd.to_datetime(frame["valid_time_utc"], utc=True)

    quantiles = frame[["p10", "p50", "p90"]].astype(float).clip(0.0, 1.0)
    low = quantiles.min(axis=1)
    high = quantiles.max(axis=1)
    frame["p10"] = low
    frame["p50"] = quantiles.sum(axis=1) - low - high
    frame["p90"] = high

    if nwp is not None and not nwp.empty:
        meta = nwp.drop_duplicates(subset="valid_time_utc")[["valid_time_utc", "source", "run_init_utc", "available_at_utc"]].copy()
        meta["valid_time_utc"] = pd.to_datetime(meta["valid_time_utc"], utc=True)
        frame = frame.merge(meta, on="valid_time_utc", how="left")
    else:
        frame["source"] = source
        frame["run_init_utc"] = pd.NaT
        frame["available_at_utc"] = pd.NaT

    frame["source"] = frame["source"].fillna(source)
    frame["issue_time_utc"] = issue_time
    frame["version"] = int(version)
    frame["valid_time_local"] = (frame["valid_time_utc"] + LOCAL_UTC_OFFSET).dt.tz_localize(None)
    frame["lead_h"] = ((frame["valid_time_utc"] - issue_time) / pd.Timedelta(hours=1)).round().astype(int)
    frame["p50_mw"] = frame["p50"] * RATED_MW_PER_TURBINE
    frame["flags"] = ""

    ordered = frame.reindex(columns=list(FORECAST_COLUMNS))
    return ordered.sort_values(["valid_time_utc", "turbine"]).reset_index(drop=True)


def run_issue(
    issue_time: datetime | pd.Timestamp,
    model: Model | None,
    providers: Providers,
    *,
    as_of: datetime | pd.Timestamp | None = None,
    version: int = 1,
    log: DecisionLog | None = None,
) -> IssueResult:
    """Один выпуск: пять шагов цикла ТЗ от погоды до анализа.

    ``as_of`` отличается от ``issue_time`` только при пересчете: момент выпуска
    остается прежним, а знание агента обновляется до времени нового прогона.
    """
    issue_time = as_utc(issue_time)
    moment = as_utc(as_of) if as_of is not None else issue_time
    log = log if log is not None else DecisionLog(issue_time)
    valid_times = horizon(issue_time)

    nwp, source, flags = _fetch_weather(moment, valid_times, providers, log)

    if nwp is None:
        log.record(
            STEP_PREPARE,
            "skip_features",
            REASON_NO_SOURCE,
            "признаки не строятся: прогона нет, работает климатология",
            as_of=moment,
        )
        predictions = providers.baseline("climatology", _empty_nwp(valid_times))
        log.record(
            STEP_RUN_MODEL,
            "use_baseline",
            REASON_NO_SOURCE,
            "прогноз по климатологии",
            as_of=moment,
            rows=int(len(predictions)),
        )
    else:
        features = providers.build_features(nwp)
        log.record(
            STEP_PREPARE,
            "build_features",
            REASON_USE_SOURCE,
            "признаки построены по прогнозу погоды",
            as_of=moment,
            rows=int(len(features)),
        )
        if model is None:
            predictions = providers.baseline("curve", nwp)
            log.record(
                STEP_RUN_MODEL,
                "use_baseline",
                REASON_NO_SOURCE,
                "модели нет, работает паспортная кривая мощности",
                as_of=moment,
                rows=int(len(predictions)),
            )
        else:
            predictions = model.predict(features)
            log.record(
                STEP_RUN_MODEL,
                "predict",
                REASON_USE_SOURCE,
                "модель посчитала квантили P10/P50/P90",
                as_of=moment,
                rows=int(len(predictions)),
            )

    forecast = _assemble_forecast(issue_time, version, predictions, nwp, source)
    log.record(
        STEP_FORECAST,
        "publish_version",
        REASON_USE_SOURCE,
        f"версия {version}: {len(forecast)} строк на горизонт +1...+{HORIZON_HOURS} ч",
        as_of=moment,
        version=int(version),
        rows=int(len(forecast)),
    )

    forecast["flags"] = risk_flags(forecast, nwp, flags)
    counts = forecast.loc[forecast["flags"] != "", "flags"].str.split("|").explode().value_counts()
    log.record(
        STEP_ANALYZE,
        "flag_hours" if not counts.empty else "no_findings",
        REASON_RISK_FLAGS,
        "проверены диапазон, рампы, отсечка и обледенение" + (f": {counts.to_dict()}" if not counts.empty else ": замечаний нет"),
        as_of=moment,
        flags={str(key): int(value) for key, value in counts.items()},
    )

    manifest = dict(providers.build_manifest(moment.to_pydatetime(), nwp if nwp is not None else _empty_nwp(valid_times)))
    manifest["version"] = int(version)
    manifest["decisions"] = log.records

    return IssueResult(
        issue_time_utc=issue_time.to_pydatetime(),
        version=int(version),
        forecast=forecast,
        manifest=manifest,
        decisions=log.records,
    )


def _default_sink(results: Sequence[IssueResult], output_dir: Path) -> None:
    """Запись файлов живет в ``src.forecast.outputs`` (задача #18)."""
    from .. import outputs

    outputs.write_outputs(results, Path(output_dir))


def replay(
    start: date | str,
    end: date | str,
    output_dir: str | Path,
    providers: Providers,
    *,
    model: Model | None = None,
    sink: IssueSink | None = None,
) -> list[IssueResult]:
    """Ретро-симуляция: выпуск на каждый день плюс пересчеты по новым прогонам.

    Шаг ``recompute_on_update``: агент просыпается на событие «прогон стал
    доступен» и публикует новую версию, только если прогноз изменился
    существенно. Несущественное изменение тоже попадает в журнал.
    """
    results: list[IssueResult] = []

    for day in pd.date_range(pd.Timestamp(start).date(), pd.Timestamp(end).date(), freq="D"):
        issue_time = issue_time_for(day)
        log = DecisionLog(issue_time)
        published = run_issue(issue_time, model, providers, log=log, version=1)
        day_results = [published]
        version = 1

        window_end = issue_time + RECOMPUTE_WINDOW
        events = providers.run_events(issue_time.to_pydatetime(), window_end.to_pydatetime())
        for event in sorted(events, key=lambda item: as_utc(item.available_at_utc)):
            moment = as_utc(event.available_at_utc)
            if moment <= issue_time or moment > window_end:
                continue

            candidate = run_issue(issue_time, model, providers, as_of=moment, log=log, version=version + 1)
            changed, reason = material_change(published.forecast, candidate.forecast)
            if changed:
                version += 1
                log.record(
                    STEP_RECOMPUTE,
                    "publish_new_version",
                    REASON_PUBLISH_NEW_VERSION,
                    reason,
                    as_of=moment,
                    version=int(version),
                    source=event.source,
                    run_init_utc=event.run_init_utc,
                )
                published = candidate
                day_results.append(candidate)
            else:
                log.record(
                    STEP_RECOMPUTE,
                    "keep_version",
                    REASON_NO_MATERIAL_CHANGE,
                    reason,
                    as_of=moment,
                    version=int(version),
                    source=event.source,
                    run_init_utc=event.run_init_utc,
                )

        for result in day_results:
            result.decisions = log.records
            result.manifest["decisions"] = log.records
        results.extend(day_results)

    (sink or _default_sink)(results, Path(output_dir))
    return results
