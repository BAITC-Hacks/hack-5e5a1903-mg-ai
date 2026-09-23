"""Правила агента: проверки прогона, флаги риска и существенность изменения.

Здесь только чистые функции из ``analyze``: ни сети, ни файлов, ни приложения.
Пороги проверяются на границе, потому что именно они решают, уйдет ли агент
на запасной источник и опубликует ли новую версию.
"""

from datetime import UTC, datetime, timedelta

import pytest

from src.modules.forecast import analyze
from src.modules.forecast.config import HORIZON_HOURS, LOCAL_OFFSET
from src.modules.forecast.schemas import ForecastHour

ISSUE_TIME = datetime(2026, 1, 31, 2, tzinfo=UTC)


def horizon(hours: int = HORIZON_HOURS) -> list[datetime]:
    return [ISSUE_TIME + timedelta(hours=lead) for lead in range(1, hours + 1)]


def points(winds: list[float], temp: float = -5.0, humidity: float | None = 60.0) -> dict[datetime, analyze.WeatherPoint]:
    return {
        moment: analyze.WeatherPoint(wind_ms=wind, temp_c=temp, humidity_pct=humidity)
        for moment, wind in zip(horizon(len(winds)), winds, strict=True)
    }


def hours(p50_values: list[float], turbine: str = "T1") -> list[ForecastHour]:
    rows = []
    for moment, p50 in zip(horizon(len(p50_values)), p50_values, strict=True):
        rows.append(
            ForecastHour(
                valid_time_utc=moment,
                valid_time_local=moment + LOCAL_OFFSET,
                lead_h=int((moment - ISSUE_TIME).total_seconds() // 3600),
                turbine=turbine,
                p10=max(0.0, p50 - 0.05),
                p50=p50,
                p90=min(1.0, p50 + 0.05),
                p50_mw=p50 * 2.5,
                wind_ms=8.0,
                temp_c=-5.0,
                source="ifs025",
                run_init_utc=ISSUE_TIME - timedelta(hours=8),
                available_at_utc=ISSUE_TIME - timedelta(minutes=30),
            )
        )
    return rows


def test_an_empty_run_is_rejected():
    assert analyze.validate_nwp({}, horizon()) == ["прогон пустой"]


def test_a_run_with_holes_is_rejected():
    problems = analyze.validate_nwp(points([7.0] * 40), horizon())

    assert problems == ["нет 8 часов горизонта"]


def test_a_frozen_run_is_rejected():
    problems = analyze.validate_nwp(points([7.0] * HORIZON_HOURS), horizon())

    assert "прогон застыл: ветер постоянный на всем горизонте" in problems


def test_wind_outside_the_physical_range_is_rejected():
    winds = [7.0 + index * 0.1 for index in range(HORIZON_HOURS)]
    winds[10] = 120.0

    assert any("ветер вне диапазона" in problem for problem in analyze.validate_nwp(points(winds), horizon()))


def test_a_good_run_has_no_problems():
    winds = [7.0 + index * 0.1 for index in range(HORIZON_HOURS)]

    assert analyze.validate_nwp(points(winds), horizon()) == []


def test_quiet_weather_leaves_no_flags():
    flags = analyze.risk_flags(hours([0.4] * 6), points([8.0] * 6))

    assert flags == [[]] * 6


def test_wind_above_cut_out_raises_the_flag():
    flags = analyze.risk_flags(hours([0.0] * 6), points([26.0] * 6))

    assert all(analyze.FLAG_CUT_OUT in row for row in flags)


def test_icing_needs_both_cold_and_humidity():
    cold_and_wet = analyze.risk_flags(hours([0.4] * 6), points([8.0] * 6, temp=0.5, humidity=95.0))
    cold_and_dry = analyze.risk_flags(hours([0.4] * 6), points([8.0] * 6, temp=0.5, humidity=40.0))

    assert all(analyze.FLAG_ICING in row for row in cold_and_wet)
    assert all(analyze.FLAG_ICING not in row for row in cold_and_dry)


def test_icing_is_not_guessed_without_humidity():
    flags = analyze.risk_flags(hours([0.4] * 6), points([8.0] * 6, temp=0.5, humidity=None))

    assert all(analyze.FLAG_ICING not in row for row in flags)


def test_a_sharp_ramp_is_flagged_three_hours_later():
    flags = analyze.risk_flags(hours([0.1, 0.1, 0.1, 0.9, 0.9, 0.9]), points([8.0] * 6))

    assert flags[3] == [analyze.FLAG_RAMP]
    assert flags[:3] == [[], [], []]


def test_base_flags_land_on_every_hour():
    flags = analyze.risk_flags(hours([0.4] * 4), points([8.0] * 4), [analyze.FLAG_DEGRADED])

    assert flags == [[analyze.FLAG_DEGRADED]] * 4


def test_spread_needs_two_sources():
    assert analyze.source_spread(points([8.0] * 4)) is None


def test_spread_is_the_width_between_sources():
    calm = points([6.0] * 4)
    windy = points([9.0] * 4)

    assert analyze.source_spread(calm, windy) == pytest.approx(3.0)


def test_ensemble_averages_the_sources():
    ensemble = analyze.ensemble_points({"a": points([6.0] * 4), "b": points([10.0] * 4)})

    assert [point.wind_ms for point in ensemble.values()] == [8.0] * 4


def test_a_first_version_is_always_material():
    changed, reason = analyze.material_change([], hours([0.4] * 4), ISSUE_TIME.date())

    assert changed
    assert reason == "прошлой версии нет"


def test_a_single_hour_moving_a_lot_forces_a_new_version():
    before = hours([0.4] * 6)
    after = hours([0.4, 0.4, 0.4, 0.4, 0.4, 0.7])

    changed, reason = analyze.material_change(before, after, (ISSUE_TIME + LOCAL_OFFSET).date())

    assert changed
    assert "час меняется" in reason


def test_a_small_change_keeps_the_version():
    before = hours([0.4] * 6)
    after = hours([0.41, 0.4, 0.4, 0.4, 0.4, 0.4])

    changed, reason = analyze.material_change(before, after, (ISSUE_TIME + LOCAL_OFFSET).date())

    assert not changed
    assert "сутки D меняются" in reason


def test_day_energy_drifting_above_the_threshold_forces_a_new_version():
    target = (ISSUE_TIME + LOCAL_OFFSET + timedelta(days=1)).date()
    before = hours([0.4] * HORIZON_HOURS)
    after = hours([0.44] * HORIZON_HOURS)

    changed, reason = analyze.material_change(before, after, target)

    assert changed
    assert "выработка суток D" in reason
