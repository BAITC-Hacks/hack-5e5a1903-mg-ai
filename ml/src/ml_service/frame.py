"""Приводит строки погоды «час × модель погоды» к широкой таблице «час × признак».

Один и тот же код работает при прогнозе и при обучении: обучающая выборка строится через
build_frame_from_rows на исторических прогнозах погоды, поэтому модель видит входы ровно
в том виде, в каком их отдаст сервис.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from ml_service.errors import invalid_input
from ml_service.schemas import WEATHER_VARIABLES, WIND_SPEED_VARIABLES, PredictRequest

HUB_HEIGHT_M = 80.0
# Степенной профиль ветра для пересчета на высоту ступицы, когда 80 м в источнике нет.
SHEAR_EXPONENT = 0.14
# Порядок важен: сначала высота ступицы, затем ближайшие к ней высоты.
_HUB_SOURCES = {"ws80": 80.0, "ws100": 100.0, "ws120": 120.0, "ws10": 10.0}
_WIND_SHIFT_COLUMNS = [*WIND_SPEED_VARIABLES, "gust10"]
_MAX_EXAMPLES = 5


@dataclass(frozen=True)
class Frame:
    issue_time: pd.Timestamp
    # Индекс valid_time_utc. Колонки: lead_h и "<source>__<переменная>", включая "<source>__hub" и "<source>__nwp_lead_h".
    wide: pd.DataFrame
    # Индекс valid_time_utc. Колонки: lead_h, wind_speed_hub_ms, wind_spread_ms, t2m, sources.
    summary: pd.DataFrame
    sources: list[str]
    hours_by_source: dict[str, int]


def to_utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def horizon_hours(issue_time: pd.Timestamp, horizon: int) -> pd.DatetimeIndex:
    return pd.date_range(issue_time + pd.Timedelta(hours=1), periods=horizon, freq="h", name="valid_time_utc")


def build_frame(request: PredictRequest) -> Frame:
    rows = pd.DataFrame([row.model_dump() for row in request.rows])
    return build_frame_from_rows(request.issue_time_utc, request.horizon_hours, rows, request.options.wind_shift_ms)


def build_frame_from_rows(issue_time, horizon: int, rows: pd.DataFrame, wind_shift_ms: float = 0.0) -> Frame:
    issue = to_utc(issue_time)
    if issue != issue.floor("h"):
        raise invalid_input("ISSUE_TIME_NOT_ON_HOUR", "issue_time_utc должен быть ровно началом часа", {"issue_time_utc": issue.isoformat()})

    rows = rows.copy()
    for column in ("valid_time_utc", "run_init_utc", "available_at_utc"):
        rows[column] = pd.to_datetime(rows[column], utc=True)
    for column in WEATHER_VARIABLES:
        rows[column] = pd.to_numeric(rows[column], errors="coerce") if column in rows else np.nan

    hours = horizon_hours(issue, horizon)
    _check_rows(rows, issue, hours)

    if wind_shift_ms:
        rows[_WIND_SHIFT_COLUMNS] = (rows[_WIND_SHIFT_COLUMNS] + wind_shift_ms).clip(lower=0)
    rows["hub"] = _hub_wind(rows)
    rows["nwp_lead_h"] = (rows["valid_time_utc"] - rows["run_init_utc"]) / pd.Timedelta(hours=1)

    covered = pd.DatetimeIndex(rows.loc[rows["hub"].notna(), "valid_time_utc"].unique())
    missing = hours.difference(covered)
    if len(missing):
        raise invalid_input(
            "INSUFFICIENT_INPUTS",
            "На часть часов горизонта нет скорости ветра ни от одной модели погоды",
            {"missing_hours": len(missing), "examples": [t.isoformat() for t in missing[:_MAX_EXAMPLES]]},
        )

    wide = rows.pivot(index="valid_time_utc", columns="source", values=[*WEATHER_VARIABLES, "hub", "nwp_lead_h"])
    wide.columns = [f"{source}__{variable}" for variable, source in wide.columns]
    wide = wide.reindex(hours)
    wide.insert(0, "lead_h", np.arange(1, horizon + 1))

    hub = rows.pivot(index="valid_time_utc", columns="source", values="hub").reindex(hours)
    temperature = rows.pivot(index="valid_time_utc", columns="source", values="t2m").reindex(hours)
    summary = pd.DataFrame(index=hours)
    summary["lead_h"] = np.arange(1, horizon + 1)
    summary["wind_speed_hub_ms"] = hub.mean(axis=1)
    summary["wind_spread_ms"] = hub.std(axis=1, ddof=0).where(hub.notna().sum(axis=1) >= 2)
    summary["t2m"] = temperature.mean(axis=1)
    summary["sources"] = [sorted(hub.columns[hub.loc[t].notna()]) for t in hours]

    hours_by_source = rows[rows["hub"].notna()].groupby("source")["valid_time_utc"].nunique().to_dict()
    return Frame(issue, wide, summary, sorted(rows["source"].unique()), {k: int(v) for k, v in hours_by_source.items()})


def _check_rows(rows: pd.DataFrame, issue: pd.Timestamp, hours: pd.DatetimeIndex) -> None:
    leaked = rows[rows["available_at_utc"] > issue]
    if len(leaked):
        raise invalid_input(
            "LEAKAGE_DETECTED",
            "Есть прогоны погоды, которые стали доступны позже момента прогноза",
            {"rows": len(leaked), "examples": _examples(leaked, ["source", "valid_time_utc", "available_at_utc"])},
        )
    inconsistent = rows[rows["run_init_utc"] > rows["available_at_utc"]]
    if len(inconsistent):
        raise invalid_input(
            "INCONSISTENT_RUN_TIMES",
            "run_init_utc не может быть позже available_at_utc",
            {"rows": len(inconsistent), "examples": _examples(inconsistent, ["source", "run_init_utc", "available_at_utc"])},
        )
    outside = rows[~rows["valid_time_utc"].isin(hours)]
    if len(outside):
        raise invalid_input(
            "OUT_OF_HORIZON",
            f"valid_time_utc должен быть началом одного из часов T+1 … T+{len(hours)}",
            {"rows": len(outside), "examples": _examples(outside, ["source", "valid_time_utc"])},
        )
    duplicated = rows[rows.duplicated(["valid_time_utc", "source"], keep=False)]
    if len(duplicated):
        raise invalid_input(
            "DUPLICATE_ROWS",
            "На один час и одну модель погоды пришло несколько строк",
            {"rows": len(duplicated), "examples": _examples(duplicated, ["source", "valid_time_utc"])},
        )


def _hub_wind(rows: pd.DataFrame) -> pd.Series:
    hub = pd.Series(np.nan, index=rows.index)
    for column, height in _HUB_SOURCES.items():
        hub = hub.fillna(rows[column] * (HUB_HEIGHT_M / height) ** SHEAR_EXPONENT)
    return hub


def _examples(rows: pd.DataFrame, columns: list[str]) -> list[dict]:
    return rows[columns].head(_MAX_EXAMPLES).astype(str).to_dict("records")
