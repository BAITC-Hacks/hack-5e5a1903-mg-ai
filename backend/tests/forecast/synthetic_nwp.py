"""Синтетический кэш прогнозов в формате ``data/nwp/<source>/*.csv.gz``.

Single Runs (``ifs``): каждый прогон по шагу источника, часы с заблаговременностью 0…horizon_h.
Previous Runs (остальные): на каждый час t строки ``prev_day`` N = 1…3 от прогона
floor_step(t) − 24·N ч, как их пишет ``fetch_prev_runs``.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast.weather.asof import CACHE_TO_OUTPUT
from src.forecast.weather.prev_runs_rule import PREV_DAYS
from src.forecast.weather.sources import Source

SINGLE_RUNS = {"ifs"}
EMPTY_COLUMNS = {"gfs": ["wind_speed_100m"], "icon": ["wind_gusts_10m"], "gem": ["wind_speed_100m", "surface_pressure"]}


def synthetic_cache(source: Source, start: str, end: str, horizon_h: int = 72, seed: int = 0) -> pd.DataFrame:
    step = pd.Timedelta(hours=source.run_step_h)
    if source.name in SINGLE_RUNS:
        inits = pd.date_range(pd.Timestamp(start, tz="UTC").floor(step), pd.Timestamp(end, tz="UTC"), freq=step)
        leads = pd.to_timedelta(np.arange(horizon_h + 1), unit="h")
        run_init = inits.repeat(len(leads))
        valid = run_init + np.tile(leads, len(inits))
        frame = pd.DataFrame({"run_init_utc": run_init, "valid_time_utc": valid})
    else:
        hours = pd.date_range(start, end, freq="h", tz="UTC")
        frame = pd.concat(
            [pd.DataFrame({"run_init_utc": hours.floor(step) - pd.Timedelta(days=n), "valid_time_utc": hours, "prev_day": n}) for n in PREV_DAYS],
            ignore_index=True,
        )

    rng = np.random.default_rng(seed)
    for column in CACHE_TO_OUTPUT:
        frame[column] = rng.normal(8.0, 3.0, len(frame)).round(2)
    for column in EMPTY_COLUMNS.get(source.name, []):
        frame[column] = np.nan
    return frame


def write_cache(frame: pd.DataFrame, root: Path, source: Source) -> None:
    folder = root / source.cache_dir
    folder.mkdir(parents=True, exist_ok=True)
    for month, part in frame.groupby(frame["valid_time_utc"].dt.strftime("%Y%m")):
        out = part.copy()
        for column in ("run_init_utc", "valid_time_utc"):
            out[column] = out[column].dt.strftime("%Y-%m-%dT%H:%MZ")
        out.to_csv(folder / f"{month}.csv.gz", index=False)
