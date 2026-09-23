"""Какой прогон стоит за ``X_previous_dayN`` в Open-Meteo Previous Runs API.

Чистые функции без сети. Правило проверено сверкой значений с Single Runs API
(точные прогоны) за август 2026 для GFS, ICON и ECMWF IFS 0.25°: значение
``X_previous_dayN`` в час t пришло из прогона init = floor_6h(t) − 24·N ч.

У ECMWF IFS 0.25° и GEM данные идут с шагом 3 ч (``temporal_resolution_seconds``
в метаданных Open-Meteo), и правило выше для них верно только в часы, кратные 3.
Open-Meteo склеивает серию ``previous_dayN`` из разных прогонов по 3-часовым
точкам, а промежуточные часы интерполирует по четырем точкам: floor_3h(t) − 3 ч
… floor_3h(t) + 6 ч. Последняя точка может прийти из следующего прогона, более
нового, чем floor_6h(t) − 24·N. Сверка с Single Runs за апрель–июнь 2026
подтверждает: в часы 04, 05, 10, 11, 16, 17, 22, 23 значение IFS 0.25° совпадает
с интерполяцией склеенной серии, а не с прогоном floor_6h(t) − 24·N. Поэтому
для таких моделей час помечается самым новым прогоном из окна интерполяции:
иначе при выпуске в него попадали бы данные прогона, еще не опубликованного.

У GEM прогоны раз в 12 ч, точных прогонов для сверки в API нет, для него шаг
прогонов 12 ч и та же поправка на 3-часовые данные.

Все моменты времени — UTC.
"""

import pandas as pd

Times = pd.Timestamp | pd.DatetimeIndex

PREV_DAYS = (1, 2, 3)


def _check_divides_day(hours: int, what: str) -> None:
    if hours <= 0 or 24 % hours:
        raise ValueError(f"{what} должен делить 24 ч, получено {hours}")


def floor_cycle(t: Times, cycle_h: int) -> Times:
    """Время запуска последнего прогона с шагом ``cycle_h`` часов, не позже ``t``.

    Прогоны идут от 00 UTC, поэтому шаг должен делить сутки без остатка.
    Работает и для одного момента, и для ``DatetimeIndex``.
    """
    _check_divides_day(cycle_h, "шаг прогонов")
    return t.floor(f"{cycle_h}h")


def newest_point(t: Times, data_step_h: int) -> Times:
    """Самая поздняя точка данных, от которой зависит значение в час ``t``.

    При шаге данных 1 ч это сам час. При шаге больше часа Open-Meteo
    интерполирует промежуточный час по четырем точкам, последняя из них
    floor(t) + 2 шага; час на сетке данных берется как есть.
    """
    _check_divides_day(data_step_h, "шаг данных")
    if data_step_h == 1:
        return t
    base = t.floor(f"{data_step_h}h")
    last = base + pd.Timedelta(hours=2 * data_step_h)
    if isinstance(t, pd.Timestamp):
        return t if t == base else last
    return t.where(t == base, last)


def run_init_for(t: Times, n: int, cycle_h: int, data_step_h: int = 1) -> Times:
    """Самый новый прогон, чьи значения вошли в ``X_previous_dayN`` в час ``t``."""
    if n not in PREV_DAYS:
        raise ValueError(f"previous_day{n} не загружается, допустимо {PREV_DAYS}")
    return floor_cycle(newest_point(t, data_step_h), cycle_h) - pd.Timedelta(hours=24 * n)


def choose_n(t: pd.Timestamp, as_of: pd.Timestamp, cycle_h: int, delay: pd.Timedelta, data_step_h: int = 1) -> int | None:
    """Минимальное N, чей прогон для часа ``t`` уже опубликован к моменту ``as_of``.

    Прогон считается опубликованным через ``delay`` после запуска. Брать
    ``previous_day1`` вслепую нельзя: для часов +25…+48 от выпуска это прогон,
    вышедший после выпуска. Если ни одно N из 1…3 не подходит, возвращает None.
    """
    for n in PREV_DAYS:
        if run_init_for(t, n, cycle_h, data_step_h) + delay <= as_of:
            return n
    return None
