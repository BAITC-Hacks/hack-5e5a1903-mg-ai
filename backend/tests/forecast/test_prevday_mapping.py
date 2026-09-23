"""Правило Previous Runs: какой прогон стоит за previous_dayN и какое N брать на выпуске."""

import pandas as pd
import pytest

from src.forecast.weather.prev_runs_rule import choose_n, floor_cycle, newest_point, run_init_for


def utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


ISSUE = utc("2026-01-31 02:00")
DELAY = pd.Timedelta(hours=7)


def test_run_init_mid_cycle():
    assert run_init_for(utc("2026-08-04 15:00"), 1, 6) == utc("2026-08-03 12:00")


@pytest.mark.parametrize(
    ("valid", "n", "expected"),
    [
        ("2026-08-04 18:00", 1, "2026-08-03 18:00"),
        ("2026-08-04 17:00", 1, "2026-08-03 12:00"),
        ("2026-08-04 00:00", 2, "2026-08-02 00:00"),
        ("2026-08-04 05:00", 3, "2026-08-01 00:00"),
    ],
)
def test_run_init_on_cycle_boundary(valid, n, expected):
    assert run_init_for(utc(valid), n, 6) == utc(expected)


@pytest.mark.parametrize(
    ("valid", "expected"),
    [
        ("2026-08-04 20:00", "2026-08-03 12:00"),
        ("2026-08-04 12:00", "2026-08-03 12:00"),
        ("2026-08-04 11:00", "2026-08-03 00:00"),
    ],
)
def test_run_init_gem_twelve_hour_cycle(valid, expected):
    assert run_init_for(utc(valid), 1, 12) == utc(expected)


def test_floor_cycle_works_on_index():
    index = pd.date_range("2026-08-04 00:00", periods=24, freq="h", tz="UTC")
    assert list(floor_cycle(index, 6)) == [floor_cycle(t, 6) for t in index]


@pytest.mark.parametrize("cycle_h", [0, 5, 7])
def test_cycle_must_divide_day(cycle_h):
    with pytest.raises(ValueError):
        floor_cycle(ISSUE, cycle_h)


def test_only_loaded_prev_days_allowed():
    with pytest.raises(ValueError):
        run_init_for(ISSUE, 4, 6)


@pytest.mark.parametrize(("lead_h", "expected"), [(1, 1), (24, 2), (25, 2), (48, 3)])
def test_choose_n_for_first_february_issue(lead_h, expected):
    valid = ISSUE + pd.Timedelta(hours=lead_h)
    n = choose_n(valid, ISSUE, 6, DELAY)
    assert n == expected
    assert run_init_for(valid, n, 6) + DELAY <= ISSUE
    if n > 1:
        assert run_init_for(valid, n - 1, 6) + DELAY > ISSUE


def test_choose_n_gem_uses_twelve_hour_cycle():
    # 20 UTC: для шага 6 ч прогон 18z накануне уже вышел бы к 02 UTC, для 12 ч берется 12z.
    valid = utc("2026-01-31 20:00")
    assert run_init_for(valid, choose_n(valid, ISSUE, 12, DELAY), 12) == utc("2026-01-30 12:00")


def test_choose_n_none_when_no_run_published():
    assert choose_n(ISSUE + pd.Timedelta(hours=72), ISSUE, 6, DELAY) is None


@pytest.mark.parametrize(
    ("valid", "cycle_h", "expected"),
    [
        # IFS 0.25°: часы, кратные 3, берутся как есть, остальные тянут точку из следующего цикла.
        ("2026-02-01 03:00", 6, "2026-01-31 00:00"),
        ("2026-02-01 01:00", 6, "2026-01-31 06:00"),
        ("2026-02-01 05:00", 6, "2026-01-31 06:00"),
        ("2026-02-01 06:00", 6, "2026-01-31 06:00"),
        # GEM: окно до 12:00 переходит в следующий 12-часовой прогон только с 07:00.
        ("2026-02-01 05:00", 12, "2026-01-31 00:00"),
        ("2026-02-01 07:00", 12, "2026-01-31 12:00"),
        ("2026-02-01 09:00", 12, "2026-01-31 00:00"),
        ("2026-02-01 11:00", 12, "2026-01-31 12:00"),
    ],
)
def test_run_init_three_hour_data_uses_newest_run_in_window(valid, cycle_h, expected):
    assert run_init_for(utc(valid), 1, cycle_h, data_step_h=3) == utc(expected)


def test_newest_point_on_index_matches_scalar():
    index = pd.date_range("2026-02-01 00:00", periods=24, freq="h", tz="UTC")
    assert list(newest_point(index, 3)) == [newest_point(t, 3) for t in index]
    assert newest_point(index, 1).equals(index)


def interpolation_window(t: pd.Timestamp) -> list[pd.Timestamp]:
    """Точки 3-часовой серии, от которых зависит час t при интерполяции Open-Meteo."""
    base = t.floor("3h")
    return [t] if t == base else [base + pd.Timedelta(hours=3 * k) for k in (-1, 0, 1, 2)]


def leaking_leads(cycle_h: int, delay: pd.Timedelta, data_step_h: int) -> list[int]:
    """Часы выпуска ISSUE, где какая-то точка окна пришла из прогона, неопубликованного к выпуску."""
    leaks = []
    for lead in range(1, 49):
        t = ISSUE + pd.Timedelta(hours=lead)
        n = choose_n(t, ISSUE, cycle_h, delay, data_step_h)
        assert n is not None
        if any(run_init_for(p, n, cycle_h) + delay > ISSUE for p in interpolation_window(t)):
            leaks.append(lead)
    return leaks


@pytest.mark.parametrize(("cycle_h", "delay_h"), [(6, 10), (12, 8)], ids=["ifs025", "gem"])
def test_choose_n_three_hour_data_never_reaches_unpublished_run(cycle_h, delay_h):
    delay = pd.Timedelta(hours=delay_h)
    assert leaking_leads(cycle_h, delay, data_step_h=3) == []
    # Без поправки на 3-часовые данные те же часы брали значения из неопубликованного прогона.
    assert len(leaking_leads(cycle_h, delay, data_step_h=1)) == 8
