"""Какой прогон стоит за ``X_previous_dayN`` в Open-Meteo Previous Runs API.

Чистые функции без сети. Правило проверено сверкой значений с Single Runs API
(точные прогоны) за август 2026 для GFS, ICON и ECMWF IFS 0.25°: значение
``X_previous_dayN`` в час t пришло из прогона init = floor_6h(t) − 24·N ч.

У GEM прогоны раз в 12 ч, а точных прогонов для сверки в API нет. Для него
берется init = floor_12h(t) − 24·N ч: это правило не может дать прогон новее
настоящего, то есть ошибается только в безопасную сторону.

Все моменты времени — UTC.
"""

import pandas as pd

Times = pd.Timestamp | pd.DatetimeIndex

PREV_DAYS = (1, 2, 3)


def floor_cycle(t: Times, cycle_h: int) -> Times:
    """Время запуска последнего прогона с шагом ``cycle_h`` часов, не позже ``t``.

    Прогоны идут от 00 UTC, поэтому шаг должен делить сутки без остатка.
    Работает и для одного момента, и для ``DatetimeIndex``.
    """
    if cycle_h <= 0 or 24 % cycle_h:
        raise ValueError(f"шаг прогонов должен делить 24 ч, получено {cycle_h}")
    return t.floor(f"{cycle_h}h")


def run_init_for(t: Times, n: int, cycle_h: int) -> Times:
    """Время запуска прогона, из которого пришло ``X_previous_dayN`` в час ``t``."""
    if n not in PREV_DAYS:
        raise ValueError(f"previous_day{n} не загружается, допустимо {PREV_DAYS}")
    return floor_cycle(t, cycle_h) - pd.Timedelta(hours=24 * n)


def choose_n(t: pd.Timestamp, as_of: pd.Timestamp, cycle_h: int, delay: pd.Timedelta) -> int | None:
    """Минимальное N, чей прогон для часа ``t`` уже опубликован к моменту ``as_of``.

    Прогон считается опубликованным через ``delay`` после запуска. Брать
    ``previous_day1`` вслепую нельзя: для часов +25…+48 от выпуска это прогон,
    вышедший после выпуска. Если ни одно N из 1…3 не подходит, возвращает None.
    """
    for n in PREV_DAYS:
        if run_init_for(t, n, cycle_h) + delay <= as_of:
            return n
    return None
