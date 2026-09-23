"""Недобор заявки диспетчера на часах бэктеста: без HTTP и без соседей."""

from datetime import UTC, datetime, timedelta

from pytest import approx

from src.modules.forecast.service import BacktestPoint, backtest_shortfall, bid_mw

ISSUE = datetime(2025, 1, 10, 2, tzinfo=UTC)


def station_hour(lead_h: int, *, p10: float, p50: float, actual: float, turbines: tuple[str, ...] = ("T1", "T2")) -> list[BacktestPoint]:
    """Час, где обе турбины 2,5 МВт дали одинаковые доли номинала."""
    valid = ISSUE + timedelta(hours=lead_h)
    return [BacktestPoint(ISSUE, valid, turbine, p10, p50, actual) for turbine in turbines]


# Станция: P10 = 1 МВт, P50 = 2 МВт. Факт 1,5 МВт в первый час и 0,5 МВт во второй.
HOURS = station_hour(17, p10=0.2, p50=0.4, actual=0.3) + station_hour(18, p10=0.2, p50=0.4, actual=0.1)


def test_bid_goes_from_p10_to_p50_with_the_risk():
    assert bid_mw(1.0, 2.0, 0.1) == 1.0
    assert bid_mw(1.0, 2.0, 0.3) == approx(1.5)
    assert bid_mw(1.0, 2.0, 0.5) == approx(2.0)


def test_a_bolder_bid_falls_short_more_often():
    careful = backtest_shortfall(HOURS, 0.1)
    bold = backtest_shortfall(HOURS, 0.5)

    assert careful.hours_share == 0.5
    assert bold.hours_share == 1.0
    assert careful.mwh_per_day == approx(0.25 * 24)
    assert bold.mwh_per_day == approx(1.0 * 24)


def test_an_hour_without_one_turbine_is_left_out():
    shortfall = backtest_shortfall(HOURS + station_hour(19, p10=0.2, p50=0.4, actual=0.0, turbines=("T1",)), 0.5)

    assert shortfall.hours == 2
    assert shortfall.hours_share == 1.0


def test_without_a_backtest_there_is_no_shortfall():
    assert backtest_shortfall([], 0.2) is None
