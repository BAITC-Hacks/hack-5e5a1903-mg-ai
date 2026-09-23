"""Логика сервиса погоды поверх ``AsOfStore`` и ``load_scada``.

Своей логики выбора прогонов здесь нет: какой прогон виден на момент ``as_of``, решает
``AsOfStore``. Сервис собирает ответы, считает ансамбль и статусы прогонов и перед отдачей
еще раз проверяет, что ни одна строка не опубликована позже ``as_of``.
"""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from functools import reduce
from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast.dataset.scada import ScadaFormatError, load_scada
from src.forecast.weather.asof import VALUE_COLUMNS, AsOfStore, LeakageError, NoRunAvailable, check_no_leakage
from src.weather_service import catalog, errors
from src.weather_service.catalog import ENSEMBLE, ENSEMBLE_MEMBERS

logger = logging.getLogger(__name__)

HOUR = pd.Timedelta(hours=1)
HORIZON_H = 48
MAX_NWP_WINDOW_H = 168
MAX_WINDOW = pd.Timedelta(days=31)
RUN_STATUSES = ("used", "stale", "after_as_of")

# Высота ступицы и приведение к ней так же, как у ML-сервиса (ml/src/ml_service/frame.py),
# чтобы ветер ансамбля на странице «Погода» совпадал с тем, что видит модель.
HUB_HEIGHT_M = 80.0
SHEAR_EXPONENT = 0.14
HUB_COLUMNS = {"ws80": 80.0, "ws100": 100.0, "ws120": 120.0}

ROW_COLUMNS = [
    "valid_time_utc",
    "source",
    "run_init_utc",
    "available_at_utc",
    "lead_h",
    "ws10",
    "ws80",
    "ws100",
    "ws120",
    "wd100",
    "gust10",
    "t2m",
    "rh2m",
    "psfc",
    "ws_spread",
    "members",
]
EXAMPLES = 5


@dataclass(frozen=True)
class WeatherData:
    """Кэш прогнозов и история турбин. Загружаются один раз на старте и дальше только читаются."""

    store: AsOfStore
    scada: pd.DataFrame | None

    @classmethod
    def load(cls, data_dir: Path) -> "WeatherData":
        store = AsOfStore(data_dir / "nwp")
        for name in store.sources:
            store.cache(name)
        try:
            scada = load_scada(data_dir)
        except (FileNotFoundError, ScadaFormatError) as exc:
            logger.warning("SCADA не загружена из %s: %s", data_dir, exc)
            scada = None
        return cls(store, scada)


# --- параметры ------------------------------------------------------------


def iso(ts: pd.Timestamp) -> str:
    return ts.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def utc(value) -> pd.Timestamp:
    """Момент из параметра запроса в UTC. Пояс уже проверен схемой запроса."""
    return pd.Timestamp(value).tz_convert("UTC")


def require_hour(name: str, ts: pd.Timestamp) -> None:
    if ts != ts.floor("h"):
        raise errors.validation_error(f"{name} должен быть началом часа", {name: iso(ts)})


def parse_sources(raw: str | None, data: WeatherData, *, default_all: bool = False) -> list[str]:
    """Список источников из ``source=gfs,icon``: без повторов, в порядке запроса."""
    known = [*data.store.sources, ENSEMBLE]
    if raw is None or not raw.strip():
        if default_all:
            return list(data.store.sources)
        raise errors.validation_error("Параметр source обязателен", {"known": known})
    names = list(dict.fromkeys(name.strip() for name in raw.split(",") if name.strip()))
    unknown = [name for name in names if name not in known]
    if unknown:
        raise errors.unknown_source(unknown, known)
    return names


def expand_ensemble(names: Iterable[str]) -> list[str]:
    """Имена настоящих источников: ``ensemble`` раскрывается в модели-участники."""
    expanded: list[str] = []
    for name in names:
        expanded.extend(ENSEMBLE_MEMBERS if name == ENSEMBLE else [name])
    return list(dict.fromkeys(expanded))


def parse_statuses(raw: str | None) -> set[str] | None:
    if raw is None or not raw.strip():
        return None
    statuses = {status.strip() for status in raw.split(",") if status.strip()}
    unknown = sorted(statuses - set(RUN_STATUSES))
    if unknown:
        raise errors.validation_error(f"Неизвестный статус прогона: {', '.join(unknown)}", {"known": list(RUN_STATUSES)})
    return statuses


def check_window(start: pd.Timestamp, end: pd.Timestamp, limit: pd.Timedelta, names: tuple[str, str]) -> None:
    if start >= end:
        raise errors.validation_error(f"{names[0]} должен быть раньше {names[1]}", {names[0]: iso(start), names[1]: iso(end)})
    if end - start > limit:
        raise errors.validation_error(f"Окно больше {limit.days} суток", {names[0]: iso(start), names[1]: iso(end)})


def nwp_hours(as_of: pd.Timestamp, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DatetimeIndex:
    """Часы окна ``/nwp``. По умолчанию это горизонт выпуска: as_of + 1 ч … as_of + 48 ч."""
    first = start if start is not None else as_of.floor("h") + HOUR
    last = end if end is not None else first + (HORIZON_H - 1) * HOUR
    require_hour("from", first)
    require_hour("to", last)
    if first > last:
        raise errors.validation_error("from должен быть не позже to", {"from": iso(first), "to": iso(last)})
    if (last - first) / HOUR + 1 > MAX_NWP_WINDOW_H:
        raise errors.validation_error(f"Окно больше {MAX_NWP_WINDOW_H} ч", {"from": iso(first), "to": iso(last)})
    return pd.date_range(first, last, freq="h")


# --- таблицы в ответ ------------------------------------------------------


def to_records(frame: pd.DataFrame, columns: list[str]) -> list[dict]:
    """Строки для ответа: пустые значения становятся None, в JSON это null."""
    part = frame.reindex(columns=columns)
    return part.astype(object).where(part.notna(), None).to_dict("records")


def hub_wind(frame: pd.DataFrame) -> pd.Series:
    """Ветер на высоте ступицы: 80 м, а без него ближайшая высота со степенным профилем."""
    hub = pd.Series(np.nan, index=frame.index)
    for column, height in HUB_COLUMNS.items():
        hub = hub.fillna(frame[column] * (HUB_HEIGHT_M / height) ** SHEAR_EXPONENT)
    return hub


def ensemble_rows(members: pd.DataFrame) -> pd.DataFrame:
    """Строки ``ensemble`` по часам из строк моделей-участников.

    ``ws80`` — среднее ветра на высоте ступицы, ``ws_spread`` — его стандартное отклонение
    (ddof=0, как у ML-сервиса). Время прогона и доступности — самые поздние среди участников.
    """
    frame = members.assign(hub=hub_wind(members))
    frame = frame[frame["hub"].notna()]
    radians = np.deg2rad(frame["wd100"])
    frame = frame.assign(wd_sin=np.sin(radians), wd_cos=np.cos(radians))
    grouped = frame.groupby("valid_time_utc", sort=True)

    means = grouped[["t2m", "rh2m", "psfc", "gust10", "wd_sin", "wd_cos"]].mean()
    out = pd.DataFrame(
        {
            "source": ENSEMBLE,
            "run_init_utc": grouped["run_init_utc"].max(),
            "available_at_utc": grouped["available_at_utc"].max(),
            "ws80": grouped["hub"].mean().round(2),
            "ws_spread": grouped["hub"].std(ddof=0).where(grouped["hub"].count() >= 2).round(2),
            "wd100": (np.rad2deg(np.arctan2(means["wd_sin"], means["wd_cos"])) % 360).round(1),
            "gust10": means["gust10"].round(2),
            "t2m": means["t2m"].round(2),
            "rh2m": means["rh2m"].round(2),
            "psfc": means["psfc"].round(2),
            "members": grouped["source"].agg(lambda names: sorted(names)),
        }
    ).reset_index()
    out["lead_h"] = ((out["valid_time_utc"] - out["run_init_utc"]) // HOUR).astype("int64")
    return out


def _fetch(data: WeatherData, name: str, as_of: pd.Timestamp, hours: pd.DatetimeIndex) -> pd.DataFrame:
    try:
        return data.store.get_nwp(name, as_of, hours)
    except LeakageError as exc:
        logger.error("Сработала проверка утечки в get_nwp(%s, %s): %s", name, iso(as_of), exc)
        raise errors.leakage_guard(str(exc)) from exc


def nwp(data: WeatherData, names: list[str], as_of: pd.Timestamp, hours: pd.DatetimeIndex) -> dict:
    """Погода по источникам на часы ``hours``, опубликованная не позже ``as_of``.

    Один источник без прогона дает ``NO_RUN_AVAILABLE``. Из нескольких источников пропускаются те,
    у которых прогона нет, а ошибка бывает, только если данных нет ни у одного.
    """
    frames: dict[str, pd.DataFrame] = {}
    lacking: dict[str, pd.DatetimeIndex] = {}
    for name in expand_ensemble(names):
        try:
            frames[name] = _fetch(data, name, as_of, hours)
        except NoRunAvailable as exc:
            lacking[name] = exc.missing

    parts, returned, missing = [], [], []
    for name in names:
        if name == ENSEMBLE:
            members = [frames[member] for member in ENSEMBLE_MEMBERS if member in frames]
            if members:
                parts.append(ensemble_rows(pd.concat(members, ignore_index=True)))
                returned.append(name)
            else:
                missing.append({"source": name, "missing_hours": len(hours)})
        elif name in frames:
            parts.append(frames[name])
            returned.append(name)
        else:
            missing.append({"source": name, "missing_hours": len(lacking[name])})

    if not parts:
        absent = reduce(pd.DatetimeIndex.union, lacking.values()) if lacking else hours
        raise errors.no_run_available(names[0] if len(names) == 1 else names, iso(as_of), [iso(t) for t in absent[:EXAMPLES]], len(absent))

    order = {name: position for position, name in enumerate(names)}
    rows = pd.concat(parts, ignore_index=True)
    rows = rows.assign(_order=rows["source"].map(order)).sort_values(["valid_time_utc", "_order"], kind="stable")
    try:
        check_no_leakage(rows, as_of)
    except LeakageError as exc:
        logger.error("Сработала проверка утечки в ответе /nwp на %s: %s", iso(as_of), exc)
        raise errors.leakage_guard(str(exc)) from exc

    return {
        "as_of_utc": as_of,
        "from_utc": hours[0],
        "to_utc": hours[-1],
        "sources": returned,
        "missing": missing,
        "rows": to_records(rows, ROW_COLUMNS),
    }


# --- прогоны --------------------------------------------------------------


def _selected(data: WeatherData, name: str, as_of: pd.Timestamp, hours: pd.DatetimeIndex) -> pd.DataFrame:
    """Строки, которые ``get_nwp`` выбрал бы на момент ``as_of``, включая часы без прогона."""
    try:
        return _fetch(data, name, as_of, hours)
    except NoRunAvailable as exc:
        covered = hours.difference(exc.missing)
        return _fetch(data, name, as_of, covered) if len(covered) else data.store.cache(name).iloc[0:0]


def runs_at(data: WeatherData, names: list[str], as_of: pd.Timestamp, issue_time: pd.Timestamp) -> list[dict]:
    """Прогоны, покрывающие горизонт выпуска ``issue_time``, со статусом относительно ``as_of``."""
    hours = pd.date_range(issue_time + HOUR, periods=HORIZON_H, freq="h")
    result = []
    for name in expand_ensemble(names):
        cache = data.store.cache(name)
        covering = cache[cache["valid_time_utc"].isin(hours)]
        if covering.empty:
            continue
        chosen = _selected(data, name, as_of, hours)
        used = chosen.groupby("run_init_utc")["lead_h"].agg(["min", "max", "size"])
        for (run_init, available_at), rows in covering.groupby(["run_init_utc", "available_at_utc"], sort=True):
            if available_at > as_of:
                status = "after_as_of"
            elif run_init in used.index:
                status = "used"
            else:
                status = "stale"
            leads = used.loc[run_init, ["min", "max"]] if status == "used" else rows["lead_h"].agg(["min", "max"])
            result.append(
                {
                    "source": name,
                    "run_init_utc": run_init,
                    "available_at_utc": available_at,
                    "status": status,
                    "lead_from_h": int(leads["min"]),
                    "lead_to_h": int(leads["max"]),
                    "hours_used": int(used.loc[run_init, "size"]) if status == "used" else 0,
                }
            )
    return sort_runs(result)


def run_events(data: WeatherData, names: list[str], start: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    """События «прогон стал доступен» в интервале ``(start, end]``."""
    events = data.store.run_events(start, end, expand_ensemble(names))
    return [
        {
            "source": event.source,
            "run_init_utc": event.run_init_utc,
            "available_at_utc": event.available_at_utc,
            "status": None,
            "lead_from_h": None,
            "lead_to_h": None,
            "hours_used": None,
        }
        for event in events
    ]


def sort_runs(runs: list[dict]) -> list[dict]:
    return sorted(runs, key=lambda run: (run["available_at_utc"], run["source"], run["run_init_utc"]))


# --- SCADA ----------------------------------------------------------------


def scada(data: WeatherData, start: pd.Timestamp, until: pd.Timestamp, turbine: str | None) -> list[dict]:
    if data.scada is None:
        raise errors.data_unavailable("SCADA не загружена: нет файлов турбин в DATA_DIR")
    check_window(start, until, MAX_WINDOW, ("from", "until"))
    history = data.scada
    mask = (history["time_utc"] >= start) & (history["time_utc"] < until)
    if turbine is not None:
        mask &= history["turbine"] == turbine
    return to_records(history.loc[mask], ["time_utc", "turbine", "power_norm", "wind_ms", "temp_c", "flag"])


# --- описание данных ------------------------------------------------------


def _span(cache: pd.DataFrame) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    if cache.empty:
        return None, None
    return cache["valid_time_utc"].min(), cache["valid_time_utc"].max()


def health(data: WeatherData) -> dict:
    sources = {}
    for name in data.store.sources:
        cache = data.store.cache(name)
        first, last = _span(cache)
        sources[name] = {"runs": int(cache["run_init_utc"].nunique()), "valid_from_utc": first, "valid_to_utc": last}
    degraded = data.scada is None or any(info["runs"] == 0 for info in sources.values())
    return {"status": "degraded" if degraded else "ok", "sources": sources, "scada_rows": 0 if data.scada is None else len(data.scada)}


def sources(data: WeatherData) -> list[dict]:
    result = []
    spans = {}
    for name, source in data.store.sources.items():
        cache = data.store.cache(name)
        spans[name] = _span(cache)
        empty = ["ws10", *(column for column in VALUE_COLUMNS if cache[column].isna().all())]
        result.append(
            {
                "source": name,
                "kind": catalog.KIND.get(name, "previous_runs"),
                "title": source.title,
                "run_step_h": source.run_step_h,
                "publication_delay_h": source.delay / HOUR,
                "delay_confirmed": catalog.DELAY_CONFIRMED.get(name, False),
                "delay_basis": source.delay_basis,
                "wind_heights_m": [int(height) for column, height in HUB_COLUMNS.items() if cache[column].notna().any()],
                "empty_columns": empty,
                "archive_from_utc": spans[name][0],
                "archive_to_utc": spans[name][1],
                "members": None,
                "wind_mae_ms": catalog.WIND_MAE_MS.get(name),
            }
        )

    members = [member for member in ENSEMBLE_MEMBERS if member in data.store.sources]
    starts = [spans[member][0] for member in members if spans[member][0] is not None]
    ends = [spans[member][1] for member in members if spans[member][1] is not None]
    result.append(
        {
            "source": ENSEMBLE,
            "kind": "ensemble",
            "title": catalog.ENSEMBLE_TITLE,
            "run_step_h": None,
            "publication_delay_h": None,
            "delay_confirmed": None,
            "delay_basis": None,
            "wind_heights_m": [int(HUB_HEIGHT_M)],
            "empty_columns": ["ws10", "ws100", "ws120"],
            "archive_from_utc": max(starts) if starts else None,
            "archive_to_utc": min(ends) if ends else None,
            "members": [{"source": member, "weight": round(1 / len(members), 4)} for member in members],
            "wind_mae_ms": catalog.WIND_MAE_MS[ENSEMBLE],
        }
    )
    return result
