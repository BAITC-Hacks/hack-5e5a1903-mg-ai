"""Шаг ``analyze``: самопроверка выпуска и сравнение версий.

Факта за февраль у нас нет, поэтому анализ это именно самопроверка: годен ли
прогон, нет ли значений вне диапазона, насколько расходятся источники, где
ожидаются погодные риски и меняется ли прогноз настолько, чтобы публиковать
новую версию.

Здесь только чистые функции: ни ввода-вывода, ни обращений наружу, ни состояния.
Пороги заданы один раз и действуют и для живого выпуска, и для заглушки.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from src.modules.forecast.config import CUT_OUT_MS, HORIZON_HOURS
from src.modules.forecast.schemas import ForecastHour

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
#: Расхождение источников по ветру, после которого выпуск помечается.
SOURCE_SPREAD_MS = 5.0
#: Пороги существенности для публикации новой версии.
ENERGY_CHANGE_SHARE = 0.03
HOUR_CHANGE_SHARE = 0.10

WIND_RANGE_MS = (0.0, 60.0)
TEMP_RANGE_C = (-60.0, 60.0)


@dataclass(frozen=True)
class WeatherPoint:
    """Погода одного часа в том виде, в каком она нужна проверкам.

    Отдельный тип затем, чтобы проверки не зависели от того, пришла погода
    из сервиса dev3 или ее придумала заглушка.
    """

    wind_ms: float
    temp_c: float
    humidity_pct: float | None = None


def validate_nwp(weather: Mapping[datetime, WeatherPoint], expected_times: Sequence[datetime]) -> list[str]:
    """Что не так с прогоном. Пустой список означает, что прогон годен."""
    if not weather:
        return ["прогон пустой"]

    problems: list[str] = []
    missing = len(set(expected_times) - set(weather))
    if missing:
        problems.append(f"нет {missing} часов горизонта")

    winds = [point.wind_ms for point in weather.values()]
    temps = [point.temp_c for point in weather.values()]
    if min(winds) < WIND_RANGE_MS[0] or max(winds) > WIND_RANGE_MS[1]:
        problems.append(f"ветер вне диапазона {WIND_RANGE_MS[0]:.0f}…{WIND_RANGE_MS[1]:.0f} м/с")
    if min(temps) < TEMP_RANGE_C[0] or max(temps) > TEMP_RANGE_C[1]:
        problems.append(f"температура вне диапазона {TEMP_RANGE_C[0]:.0f}…{TEMP_RANGE_C[1]:.0f} °C")
    if len(winds) >= HORIZON_HOURS and len(set(winds)) == 1:
        problems.append("прогон застыл: ветер постоянный на всем горизонте")

    return problems


def source_spread(*sources: Mapping[datetime, WeatherPoint]) -> float | None:
    """Среднее расхождение источников по ветру, м/с.

    Для двух источников это средняя разница по общим часам, для нескольких —
    средняя ширина вилки «самый ветреный минус самый спокойный».
    ``None``, если сравнивать не с чем.
    """
    if len(sources) < 2:
        return None
    common = sorted(set.intersection(*(set(source) for source in sources)))
    if not common:
        return None
    total = 0.0
    for moment in common:
        winds = [source[moment].wind_ms for source in sources]
        total += max(winds) - min(winds)
    return total / len(common)


def ensemble_points(by_source: Mapping[str, Mapping[datetime, WeatherPoint]]) -> dict[datetime, WeatherPoint]:
    """Погода ансамбля: среднее по источникам на каждый час.

    Эти значения видит пользователь в колонках ветра и температуры, на них же
    стоят проверки на отсечку и обледенение.
    """
    collected: dict[datetime, list[WeatherPoint]] = {}
    for points in by_source.values():
        for moment, point in points.items():
            collected.setdefault(moment, []).append(point)

    result: dict[datetime, WeatherPoint] = {}
    for moment, points in collected.items():
        humidity = [point.humidity_pct for point in points if point.humidity_pct is not None]
        result[moment] = WeatherPoint(
            wind_ms=round(sum(point.wind_ms for point in points) / len(points), 2),
            temp_c=round(sum(point.temp_c for point in points) / len(points), 1),
            humidity_pct=round(sum(humidity) / len(humidity), 1) if humidity else None,
        )
    return result


def _ramp_keys(hours: Sequence[ForecastHour]) -> set[tuple[str, datetime]]:
    """Пары «турбина, час», где мощность меняется слишком резко."""
    keys: set[tuple[str, datetime]] = set()
    by_turbine: dict[str, list[ForecastHour]] = {}
    for hour in hours:
        by_turbine.setdefault(hour.turbine, []).append(hour)

    for turbine, rows in by_turbine.items():
        ordered = sorted(rows, key=lambda row: row.valid_time_utc)
        for position in range(RAMP_WINDOW_HOURS, len(ordered)):
            if abs(ordered[position].p50 - ordered[position - RAMP_WINDOW_HOURS].p50) > RAMP_SHARE_OF_RATED:
                keys.add((turbine, ordered[position].valid_time_utc))
    return keys


def risk_flags(
    hours: Sequence[ForecastHour],
    weather: Mapping[datetime, WeatherPoint] | None = None,
    base_flags: Iterable[str] = (),
) -> list[list[str]]:
    """Флаги риска на каждый час прогноза, в том же порядке, что и ``hours``."""
    base = set(base_flags)
    ramps = _ramp_keys(hours)
    weather = weather or {}

    result: list[list[str]] = []
    for hour in hours:
        flags = set(base)
        point = weather.get(hour.valid_time_utc)
        if point is not None:
            if point.wind_ms > CUT_OUT_MS:
                flags.add(FLAG_CUT_OUT)
            if point.temp_c <= ICING_TEMP_C and point.humidity_pct is not None and point.humidity_pct > ICING_HUMIDITY_PCT:
                flags.add(FLAG_ICING)
        if (hour.turbine, hour.valid_time_utc) in ramps:
            flags.add(FLAG_RAMP)
        result.append(sorted(flags))
    return result


def flag_counts(hours: Sequence[ForecastHour]) -> dict[str, int]:
    """Сколько часов получил каждый флаг. Для журнала и для паспорта выпуска."""
    counts: dict[str, int] = {}
    for hour in hours:
        for flag in hour.flags:
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items()))


def _day_energy(hours: Sequence[ForecastHour], keys: set[tuple[str, datetime]], target_date: date) -> float:
    """Выработка суток D по общим часам, в долях номинала."""
    return sum(hour.p50 for hour in hours if (hour.turbine, hour.valid_time_utc) in keys and hour.valid_time_local.date() == target_date)


def material_change(previous: Sequence[ForecastHour], current: Sequence[ForecastHour], target_date: date) -> tuple[bool, str]:
    """Нужна ли новая версия прогноза: изменение суток D или отдельного часа."""
    if not previous:
        return True, "прошлой версии нет"

    before = {(hour.turbine, hour.valid_time_utc): hour.p50 for hour in previous}
    after = {(hour.turbine, hour.valid_time_utc): hour.p50 for hour in current}
    common = set(before) & set(after)
    if not common:
        return True, "общих часов с прошлой версией нет"

    max_hour = max(abs(after[key] - before[key]) for key in common)
    if max_hour > HOUR_CHANGE_SHARE:
        return True, f"час меняется на {max_hour:.0%} номинала при пороге {HOUR_CHANGE_SHARE:.0%}"

    energy_before = _day_energy(previous, common, target_date)
    energy_after = _day_energy(current, common, target_date)
    if energy_before <= 0:
        share = 1.0 if energy_after > 0 else 0.0
    else:
        share = abs(energy_after - energy_before) / energy_before
    if share > ENERGY_CHANGE_SHARE:
        return True, f"выработка суток D меняется на {share:.1%} при пороге {ENERGY_CHANGE_SHARE:.0%}"

    return False, f"сутки D меняются на {share:.1%}, максимум по часу {max_hour:.1%} номинала"
