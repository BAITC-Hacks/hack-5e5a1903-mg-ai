"""Заглушки погоды и модели для тестов агента.

Настоящие реализации пишут dev3 (``weather``) и dev2 (``ml``). Агент от них
не зависит: ему достаточно объекта ``Providers``, поэтому здесь лежит
маленькая подделка, которой хватает, чтобы проверить решения агента.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from src.forecast.config import CUT_IN_MS, CUT_OUT_MS, RATED_MS, TURBINES
from src.forecast.contract import NWP_COLUMNS, RunAvailable


def power_curve(wind_ms: float) -> float:
    """Паспортная кривая GW109, приведенная к долям номинала."""
    if wind_ms < CUT_IN_MS or wind_ms >= CUT_OUT_MS:
        return 0.0
    if wind_ms >= RATED_MS:
        return 1.0
    return ((wind_ms - CUT_IN_MS) / (RATED_MS - CUT_IN_MS)) ** 3


@dataclass
class FakeRun:
    """Один прогон погоды: когда посчитан, когда опубликован и какой в нем ветер."""

    source: str
    run_init: pd.Timestamp
    available_at: pd.Timestamp
    wind: float | Sequence[float] = 8.0
    temp_c: float = 5.0
    humidity_pct: float = 50.0

    def wind_series(self, hours: int) -> list[float]:
        if isinstance(self.wind, int | float):
            # Небольшой наклон, иначе проверка «прогон застыл» считает прогон негодным.
            return [float(self.wind) + 0.01 * index for index in range(hours)]
        values = [float(value) for value in self.wind]
        if len(values) < hours:
            values += [values[-1]] * (hours - len(values))
        return values[:hours]


@dataclass
class FakeWeather:
    """As-of хранилище: отдает самый свежий прогон, доступный к моменту запроса."""

    runs: list[FakeRun] = field(default_factory=list)
    calls: list[tuple[str, pd.Timestamp]] = field(default_factory=list)

    def get_nwp(self, source: str, as_of: datetime, valid_times: pd.DatetimeIndex) -> pd.DataFrame:
        from src.forecast.contract import NoRunAvailable

        moment = pd.Timestamp(as_of)
        self.calls.append((source, moment))
        available = [run for run in self.runs if run.source == source and run.available_at <= moment]
        if not available:
            raise NoRunAvailable(f"нет прогона {source} на {moment.isoformat()}")

        run = max(available, key=lambda item: item.run_init)
        wind = run.wind_series(len(valid_times))
        frame = pd.DataFrame(
            {
                "valid_time_utc": pd.DatetimeIndex(valid_times),
                "source": source,
                "run_init_utc": run.run_init,
                "available_at_utc": run.available_at,
                "lead_h": [(time - run.run_init) / pd.Timedelta(hours=1) for time in valid_times],
                "ws80": [value * 0.95 for value in wind],
                "ws100": wind,
                "ws120": [value * 1.03 for value in wind],
                "wd100": 270.0,
                "gust10": [value * 1.4 for value in wind],
                "t2m": run.temp_c,
                "rh2m": run.humidity_pct,
                "psfc": 900.0,
            }
        )
        return frame.reindex(columns=list(NWP_COLUMNS))

    def run_events(self, start: datetime, end: datetime) -> list[RunAvailable]:
        left, right = pd.Timestamp(start), pd.Timestamp(end)
        return [
            RunAvailable(source=run.source, run_init_utc=run.run_init.to_pydatetime(), available_at_utc=run.available_at.to_pydatetime())
            for run in self.runs
            if left < run.available_at <= right
        ]

    def build_manifest(self, issue_time: datetime, nwp: pd.DataFrame) -> dict:
        used = sorted(set(nwp["source"].dropna())) if not nwp.empty else []
        return {
            "issue_time_utc": pd.Timestamp(issue_time).isoformat(),
            "sources": used,
            "max_available_at_utc": (pd.Timestamp(nwp["available_at_utc"].max()).isoformat() if not nwp.empty else None),
        }


def build_features(nwp: pd.DataFrame) -> pd.DataFrame:
    """Заглушка признаков: прогноз погоды как есть."""
    return nwp.copy()


def baseline(kind: str, nwp: pd.DataFrame) -> pd.DataFrame:
    """Бейзлайн: паспортная кривая по ветру, климатология — константа."""
    times = pd.to_datetime(nwp["valid_time_utc"], utc=True)
    if kind == "climatology" or "ws100" not in nwp or nwp["ws100"].isna().all():
        values = [0.3] * len(times)
    else:
        values = [power_curve(float(wind)) for wind in nwp["ws100"]]
    return _predictions(times, values)


class FakeModel:
    """Модель, которая считает по той же кривой, что и бейзлайн."""

    def predict(self, features: pd.DataFrame) -> pd.DataFrame:
        times = pd.to_datetime(features["valid_time_utc"], utc=True)
        values = [power_curve(float(wind)) for wind in features["ws100"]]
        return _predictions(times, values)


def _predictions(times: pd.Series, values: list[float]) -> pd.DataFrame:
    rows = []
    for turbine in TURBINES:
        for time, value in zip(times, values, strict=True):
            rows.append(
                {
                    "valid_time_utc": time,
                    "turbine": turbine.name,
                    "p10": max(0.0, value - 0.1),
                    "p50": value,
                    "p90": min(1.0, value + 0.1),
                }
            )
    return pd.DataFrame(rows)


def providers_for(runs: list[FakeRun]) -> tuple[object, FakeWeather]:
    """Готовый ``Providers`` поверх подделки погоды."""
    from src.forecast.agent import Providers

    weather = FakeWeather(runs=runs)
    return (
        Providers(
            get_nwp=weather.get_nwp,
            run_events=weather.run_events,
            build_manifest=weather.build_manifest,
            build_features=build_features,
            baseline=baseline,
        ),
        weather,
    )
