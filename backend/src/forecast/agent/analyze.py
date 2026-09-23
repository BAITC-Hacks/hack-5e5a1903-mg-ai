"""Шаг ``analyze`` и сравнение версий прогноза.

Факта за февраль у нас нет, поэтому анализ это самопроверка: диапазоны значений,
целостность прогона, резкие рампы, расхождение источников и погодные риски.
Здесь только чистые функции: ни ввода-вывода, ни обращений наружу.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta

import pandas as pd

from ..config import CUT_OUT_MS, HORIZON_HOURS, LOCAL_UTC_OFFSET
from ..contract import NWP_WEATHER_COLUMNS

FLAG_CUT_OUT = "cut_out_risk"
FLAG_ICING = "icing_risk"
FLAG_RAMP = "ramp"
FLAG_SOURCE_SPREAD = "source_spread"
FLAG_DEGRADED = "degraded"

#: Обледенение: около нуля и высокая влажность.
ICING_TEMP_C = 1.0
ICING_HUMIDITY_PCT = 90.0
#: Рампа: изменение мощности больше 40% номинала за 3 часа.
RAMP_SHARE_OF_RATED = 0.40
RAMP_WINDOW_HOURS = 3
#: Расхождение источников по ветру, после которого прогноз помечается.
SOURCE_SPREAD_MS = 5.0
#: Пороги существенности для публикации новой версии.
ENERGY_CHANGE_SHARE = 0.03
HOUR_CHANGE_SHARE = 0.10

_WIND_RANGE_MS = (0.0, 60.0)
_TEMP_RANGE_C = (-60.0, 60.0)


def validate_nwp(nwp: pd.DataFrame | None, valid_times: pd.DatetimeIndex) -> list[str]:
    """Что не так с прогоном. Пустой список означает, что прогон годен."""
    if nwp is None or nwp.empty:
        return ["прогон пустой"]

    problems: list[str] = []
    have = set(pd.to_datetime(nwp["valid_time_utc"], utc=True))
    missing = len(set(valid_times) - have)
    if missing:
        problems.append(f"нет {missing} часов горизонта")

    present = [c for c in NWP_WEATHER_COLUMNS if c in nwp.columns]
    if nwp[present].isna().to_numpy().any():
        problems.append("пропуски в переменных прогноза")

    wind = nwp["ws100"].astype(float)
    if wind.min() < _WIND_RANGE_MS[0] or wind.max() > _WIND_RANGE_MS[1]:
        problems.append("ветер вне диапазона 0…60 м/с")

    temp = nwp["t2m"].astype(float)
    if temp.min() < _TEMP_RANGE_C[0] or temp.max() > _TEMP_RANGE_C[1]:
        problems.append("температура вне диапазона -60…60 °C")

    if len(wind) >= HORIZON_HOURS and wind.nunique() == 1:
        problems.append("прогон застыл: ветер постоянный на всем горизонте")

    return problems


def source_spread(primary: pd.DataFrame, other: pd.DataFrame) -> float | None:
    """Среднее расхождение источников по ветру на 100 м, м/с."""
    if primary is None or other is None or primary.empty or other.empty:
        return None
    left = primary.drop_duplicates(subset="valid_time_utc").set_index(
        pd.to_datetime(primary.drop_duplicates(subset="valid_time_utc")["valid_time_utc"], utc=True)
    )["ws100"]
    right = other.drop_duplicates(subset="valid_time_utc").set_index(
        pd.to_datetime(other.drop_duplicates(subset="valid_time_utc")["valid_time_utc"], utc=True)
    )["ws100"]
    common = left.index.intersection(right.index)
    if common.empty:
        return None
    return float((left.loc[common].astype(float) - right.loc[common].astype(float)).abs().mean())


def _ramp_keys(forecast: pd.DataFrame) -> set[tuple[str, pd.Timestamp]]:
    """Пары «турбина, час», где мощность меняется слишком резко."""
    keys: set[tuple[str, pd.Timestamp]] = set()
    frame = forecast.copy()
    frame["valid_time_utc"] = pd.to_datetime(frame["valid_time_utc"], utc=True)
    for turbine, group in frame.sort_values("valid_time_utc").groupby("turbine"):
        jump = group["p50"].astype(float).diff(RAMP_WINDOW_HOURS).abs()
        for time in group.loc[jump > RAMP_SHARE_OF_RATED, "valid_time_utc"]:
            keys.add((str(turbine), pd.Timestamp(time)))
    return keys


def risk_flags(forecast: pd.DataFrame, nwp: pd.DataFrame | None, base_flags: Iterable[str] = ()) -> pd.Series:
    """Флаги риска на каждую строку прогноза, склеенные через ``|``."""
    base = set(base_flags)
    rows: list[set[str]] = [set(base) for _ in range(len(forecast))]
    times = pd.to_datetime(forecast["valid_time_utc"], utc=True)

    weather = None
    if nwp is not None and not nwp.empty:
        weather = nwp.drop_duplicates(subset="valid_time_utc").copy()
        weather["valid_time_utc"] = pd.to_datetime(weather["valid_time_utc"], utc=True)
        weather = weather.set_index("valid_time_utc")

    ramps = _ramp_keys(forecast)

    for position, (time, turbine) in enumerate(zip(times, forecast["turbine"], strict=True)):
        if weather is not None and time in weather.index:
            row = weather.loc[time]
            if float(row["ws100"]) > CUT_OUT_MS:
                rows[position].add(FLAG_CUT_OUT)
            if float(row["t2m"]) <= ICING_TEMP_C and float(row["rh2m"]) > ICING_HUMIDITY_PCT:
                rows[position].add(FLAG_ICING)
        if (str(turbine), pd.Timestamp(time)) in ramps:
            rows[position].add(FLAG_RAMP)

    return pd.Series(["|".join(sorted(flags)) for flags in rows], index=forecast.index, dtype="object")


def day_d_date(forecast: pd.DataFrame) -> date:
    """Сутки D: следующий местный день после дня выпуска."""
    issue = pd.to_datetime(forecast["issue_time_utc"], utc=True).min()
    return (issue + LOCAL_UTC_OFFSET).date() + timedelta(days=1)


def _day_d_energy(forecast: pd.DataFrame, keys: pd.MultiIndex, target: date) -> float:
    """Суммарная выработка суток D по общим часам, в долях номинала."""
    day = forecast.set_index(["turbine", "valid_time_utc"]).loc[keys]
    local = pd.to_datetime(day["valid_time_local"])
    return float(day.loc[(local.dt.date == target).to_numpy(), "p50"].astype(float).sum())


def material_change(previous: pd.DataFrame, current: pd.DataFrame) -> tuple[bool, str]:
    """Нужна ли новая версия прогноза: изменение суток D или отдельного часа."""
    if previous is None or previous.empty:
        return True, "прошлой версии нет"

    left = previous.copy()
    right = current.copy()
    for frame in (left, right):
        frame["valid_time_utc"] = pd.to_datetime(frame["valid_time_utc"], utc=True)

    left_p50 = left.set_index(["turbine", "valid_time_utc"])["p50"].astype(float)
    right_p50 = right.set_index(["turbine", "valid_time_utc"])["p50"].astype(float)
    common = left_p50.index.intersection(right_p50.index)
    if common.empty:
        return True, "общих часов с прошлой версией нет"

    max_hour = float((right_p50.loc[common] - left_p50.loc[common]).abs().max())
    if max_hour > HOUR_CHANGE_SHARE:
        return True, f"час меняется на {max_hour:.0%} номинала при пороге {HOUR_CHANGE_SHARE:.0%}"

    target = day_d_date(right)
    before = _day_d_energy(left, common, target)
    after = _day_d_energy(right, common, target)
    if before <= 0:
        share = 1.0 if after > 0 else 0.0
    else:
        share = abs(after - before) / before
    if share > ENERGY_CHANGE_SHARE:
        return True, f"выработка суток D меняется на {share:.1%} при пороге {ENERGY_CHANGE_SHARE:.0%}"

    return False, f"сутки D меняются на {share:.1%}, максимум по часу {max_hour:.1%} номинала"
