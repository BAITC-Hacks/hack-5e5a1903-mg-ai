"""Погода строго на момент прогноза.

Любое чтение прогнозов погоды идет через ``AsOfStore``. Хранилище знает, когда каждый
прогон стал доступен (``run_init_utc`` плюс задержка источника из ``sources.py``),
и для момента ``as_of`` отдает только прогоны, опубликованные не позже него.
Это единственное место, где решается, какой прогон видит прогноз, поэтому здесь же
стоит финальная проверка на утечку будущего.

Кэш источника лежит в ``<cache_root>/<source.cache_dir>/*.csv.gz`` в длинном формате:
строка на пару (прогон, час), колонки ``run_init_utc``, ``valid_time_utc`` и переменные
Open-Meteo. Кэш читается один раз на экземпляр хранилища.

Строка прогона без скорости ветра ни на одной высоте считается отсутствующей: на такой
час берется более старый прогон с ветром. Иначе сбой архива (IFS, 04–09.08.2025) давал
бы свежий прогон с пустым ветром, а без ветра модель прогноз не строит.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast.weather.sources import SOURCES, Source

logger = logging.getLogger(__name__)

CACHE_TO_OUTPUT = {
    "wind_speed_80m": "ws80",
    "wind_speed_100m": "ws100",
    "wind_speed_120m": "ws120",
    "wind_direction_100m": "wd100",
    "wind_gusts_10m": "gust10",
    "temperature_2m": "t2m",
    "relative_humidity_2m": "rh2m",
    "surface_pressure": "psfc",
}
VALUE_COLUMNS = list(CACHE_TO_OUTPUT.values())
WIND_COLUMNS = ["ws80", "ws100", "ws120"]
OUTPUT_COLUMNS = ["valid_time_utc", "source", "run_init_utc", "available_at_utc", "lead_h", *VALUE_COLUMNS]
REQUIRED_CACHE_COLUMNS = ("run_init_utc", "valid_time_utc")

HOUR = pd.Timedelta(hours=1)


class LeakageError(RuntimeError):
    """В выход попала строка прогона, опубликованного позже момента прогноза."""


class NoRunAvailable(LookupError):
    """На момент прогноза нет опубликованного прогона хотя бы для одного из запрошенных часов."""

    def __init__(self, source: str | Sequence[str], as_of: pd.Timestamp, missing: pd.DatetimeIndex):
        self.source = source
        self.as_of = as_of
        self.missing = missing
        names = source if isinstance(source, str) else ", ".join(source)
        span = f"{missing[0]:%Y-%m-%d %H:%M}…{missing[-1]:%Y-%m-%d %H:%M}" if len(missing) else "—"
        super().__init__(f"Нет прогона {names} на {as_of:%Y-%m-%d %H:%M} UTC для {len(missing)} ч ({span})")


@dataclass(frozen=True)
class RunAvailable:
    source: str
    run_init_utc: pd.Timestamp
    available_at_utc: pd.Timestamp


@dataclass(frozen=True)
class _SourceData:
    frame: pd.DataFrame
    valid_ns: np.ndarray
    available_ns: np.ndarray
    runs: pd.DataFrame


def to_utc(value: datetime | pd.Timestamp | str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        raise ValueError(f"Время {value!r} без часового пояса, нужен UTC")
    return ts.tz_convert("UTC")


def _to_utc_index(values: Iterable[datetime | pd.Timestamp | str]) -> pd.DatetimeIndex:
    index = pd.DatetimeIndex(list(values))
    if len(index) == 0:
        raise ValueError("Пустой список часов valid_times")
    if index.tz is None:
        raise ValueError("Часы valid_times без часового пояса, нужен UTC")
    return index.tz_convert("UTC").as_unit("ns").unique().sort_values()


def _naive_ns(values: pd.Series | pd.DatetimeIndex) -> np.ndarray:
    if isinstance(values, pd.Series):
        values = pd.DatetimeIndex(values)
    return values.tz_convert("UTC").tz_localize(None).as_unit("ns").to_numpy()


def check_no_leakage(frame: pd.DataFrame, as_of: datetime | pd.Timestamp) -> None:
    """Бросает ``LeakageError``, если хоть одна строка опубликована позже ``as_of``."""
    as_of = to_utc(as_of)
    leaked = frame["available_at_utc"] > as_of
    if leaked.any():
        first = frame.loc[leaked].iloc[0]
        raise LeakageError(
            f"{int(leaked.sum())} строк опубликованы позже {as_of:%Y-%m-%d %H:%M} UTC, "
            f"например {first['source']} прогон {first['run_init_utc']:%Y-%m-%d %H:%M} доступен в {first['available_at_utc']:%Y-%m-%d %H:%M}"
        )


def _empty_frame() -> pd.DataFrame:
    utc = "datetime64[ns, UTC]"
    frame = pd.DataFrame({"valid_time_utc": pd.Series(dtype=utc), "source": pd.Series(dtype="str")})
    frame["run_init_utc"] = pd.Series(dtype=utc)
    frame["available_at_utc"] = pd.Series(dtype=utc)
    frame["lead_h"] = pd.Series(dtype="int64")
    for column in VALUE_COLUMNS:
        frame[column] = pd.Series(dtype="float64")
    return frame


def load_source_cache(source: Source, cache_root: Path) -> pd.DataFrame:
    """Читает кэш источника и приводит его к колонкам выхода ``get_nwp``, сортировка по (час, прогон)."""
    files = sorted((cache_root / source.cache_dir).glob("*.csv.gz"))
    if not files:
        logger.warning("Кэш погоды %s пуст: нет файлов в %s", source.name, cache_root / source.cache_dir)
        return _empty_frame()

    raw = pd.concat([pd.read_csv(path) for path in files], ignore_index=True)
    absent = [column for column in REQUIRED_CACHE_COLUMNS if column not in raw.columns]
    if absent:
        raise ValueError(f"В кэше {source.name} нет колонок {absent}")

    frame = pd.DataFrame(
        {
            "valid_time_utc": pd.to_datetime(raw["valid_time_utc"], utc=True, format="ISO8601").dt.as_unit("ns"),
            "source": source.name,
            "run_init_utc": pd.to_datetime(raw["run_init_utc"], utc=True, format="ISO8601").dt.as_unit("ns"),
        }
    )
    for cache_column, column in CACHE_TO_OUTPUT.items():
        frame[column] = pd.to_numeric(raw[cache_column], errors="coerce").astype("float64") if cache_column in raw.columns else np.nan
    frame = frame.dropna(subset=["valid_time_utc", "run_init_utc"])

    step = pd.Timedelta(hours=source.run_step_h)
    off_grid = (frame["run_init_utc"] - frame["run_init_utc"].dt.floor("D")) % step != pd.Timedelta(0)
    if off_grid.any():
        logger.warning(
            "Кэш %s: %d строк с прогонами вне шага %d ч отброшены (%s)",
            source.name,
            int(off_grid.sum()),
            source.run_step_h,
            ", ".join(sorted(frame.loc[off_grid, "run_init_utc"].dt.strftime("%Hz").unique())),
        )
        frame = frame.loc[~off_grid]

    duplicated = frame.duplicated(["run_init_utc", "valid_time_utc"], keep="last")
    if duplicated.any():
        logger.warning("Кэш %s: %d повторов (прогон, час), оставлена последняя запись", source.name, int(duplicated.sum()))
        frame = frame.loc[~duplicated]

    no_wind = frame[WIND_COLUMNS].isna().all(axis=1)
    if no_wind.any():
        logger.warning(
            "Кэш %s: %d строк без скорости ветра отброшены, на эти часы возьмется более старый прогон (%s)",
            source.name,
            int(no_wind.sum()),
            ", ".join(sorted(frame.loc[no_wind, "run_init_utc"].dt.strftime("%Y-%m-%d %Hz").unique())),
        )
        frame = frame.loc[~no_wind]

    frame["available_at_utc"] = frame["run_init_utc"] + source.delay
    frame["lead_h"] = ((frame["valid_time_utc"] - frame["run_init_utc"]) // HOUR).astype("int64")
    return frame[OUTPUT_COLUMNS].sort_values(["valid_time_utc", "run_init_utc"], kind="stable").reset_index(drop=True)


class AsOfStore:
    """Прогнозы погоды в том виде, в каком они были доступны на момент ``as_of``."""

    def __init__(self, cache_root: Path | str, sources: Mapping[str, Source] | None = None):
        self.cache_root = Path(cache_root)
        self.sources = dict(SOURCES if sources is None else sources)
        self._data: dict[str, _SourceData] = {}

    def _source(self, name: str) -> Source:
        if name not in self.sources:
            raise ValueError(f"Неизвестный источник погоды {name!r}, есть: {', '.join(self.sources)}")
        return self.sources[name]

    def _load(self, name: str) -> _SourceData:
        if name not in self._data:
            frame = load_source_cache(self._source(name), self.cache_root)
            runs = frame[["run_init_utc", "available_at_utc"]].drop_duplicates().sort_values("run_init_utc").reset_index(drop=True)
            self._data[name] = _SourceData(frame, _naive_ns(frame["valid_time_utc"]), _naive_ns(frame["available_at_utc"]), runs)
        return self._data[name]

    def cache(self, name: str) -> pd.DataFrame:
        """Весь кэш источника в колонках выхода ``get_nwp``, без отбора по as_of. Только для чтения.

        Нужен для описания архива: какие прогоны покрывают часы, какие колонки пустые.
        Отдавать эти строки в прогноз нельзя: в них есть прогоны, опубликованные позже любого момента.
        """
        return self._load(name).frame

    def get_nwp(self, source: str, as_of: datetime | pd.Timestamp, valid_times: Iterable[datetime | pd.Timestamp]) -> pd.DataFrame:
        """Для каждого часа из ``valid_times`` строка самого свежего прогона с ``available_at_utc <= as_of`` и ветром.

        Нет прогона хотя бы для одного часа — ``NoRunAvailable``.
        """
        as_of = to_utc(as_of)
        wanted = _to_utc_index(valid_times)
        data = self._load(source)

        wanted_ns = _naive_ns(wanted)
        as_of_ns = _naive_ns(pd.DatetimeIndex([as_of]))[0]
        lo = np.searchsorted(data.valid_ns, wanted_ns[0], side="left")
        hi = np.searchsorted(data.valid_ns, wanted_ns[-1], side="right")
        mask = (data.available_ns[lo:hi] <= as_of_ns) & np.isin(data.valid_ns[lo:hi], wanted_ns)
        # Кадр отсортирован по (час, прогон), поэтому последняя строка часа — самый свежий доступный прогон.
        chosen = data.frame.iloc[lo:hi].loc[mask].drop_duplicates("valid_time_utc", keep="last").reset_index(drop=True)

        missing = wanted.difference(pd.DatetimeIndex(chosen["valid_time_utc"]))
        if len(missing):
            raise NoRunAvailable(source, as_of, missing)
        check_no_leakage(chosen, as_of)
        return chosen

    def get_nwp_multi(self, sources: Sequence[str], as_of: datetime | pd.Timestamp, valid_times: Iterable[datetime | pd.Timestamp]) -> pd.DataFrame:
        """Длинная таблица ``get_nwp`` по нескольким источникам. Источник без прогона пропускается."""
        if not sources:
            raise ValueError("Пустой список источников")
        as_of = to_utc(as_of)
        wanted = _to_utc_index(valid_times)
        frames = []
        for name in sources:
            try:
                frames.append(self.get_nwp(name, as_of, wanted))
            except NoRunAvailable as exc:
                logger.warning("Источник %s пропущен: %s", name, exc)
        if not frames:
            raise NoRunAvailable(list(sources), as_of, wanted)
        result = pd.concat(frames, ignore_index=True)
        check_no_leakage(result, as_of)
        return result

    def run_events(self, start: datetime | pd.Timestamp, end: datetime | pd.Timestamp, sources: Sequence[str] | None = None) -> list[RunAvailable]:
        """Прогоны из кэша, ставшие доступными в интервале ``(start, end]``, по времени доступности."""
        start, end = to_utc(start), to_utc(end)
        events = []
        for name in self.sources if sources is None else sources:
            runs = self._load(name).runs
            inside = runs.loc[(runs["available_at_utc"] > start) & (runs["available_at_utc"] <= end)]
            events.extend(
                RunAvailable(name, init, available) for init, available in zip(inside["run_init_utc"], inside["available_at_utc"], strict=True)
            )
        return sorted(events, key=lambda event: (event.available_at_utc, event.source, event.run_init_utc))
