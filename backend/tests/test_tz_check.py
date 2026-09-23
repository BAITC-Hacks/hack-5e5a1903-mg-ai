"""Проверка часового пояса SCADA (#10): сама методика на синтетике, без сети и без кэша."""

import numpy as np
import pandas as pd
import pytest

from src.analysis.tz_check import centered_hourly, offset_correlations, peak_estimate, temp_shift_check


def test_offset_correlations_find_the_true_offset():
    rng = np.random.default_rng(0)
    times = pd.date_range("2024-01-01", periods=2000, freq="h")
    # Гладкий «ветер» в UTC и тот же ветер, записанный по часам UTC+6, с шумом.
    wind_utc = pd.Series(np.convolve(rng.normal(size=2000 + 11), np.ones(12) / 12, mode="valid"), index=times)
    scada = pd.Series(wind_utc.to_numpy() + rng.normal(scale=0.05, size=2000), index=times + pd.Timedelta(hours=6))

    corrs = offset_correlations(scada, wind_utc, range(3, 10))

    assert int(corrs.idxmax()) == 6
    assert peak_estimate(corrs) == pytest.approx(6, abs=0.1)


def test_peak_estimate_between_hours():
    offsets = np.arange(3, 10)
    corrs = pd.Series(1 - (offsets - 5.7) ** 2 / 10, index=offsets)

    assert peak_estimate(corrs) == pytest.approx(5.7)


def test_centered_hour_takes_records_from_half_hour_before():
    times = pd.date_range("2024-01-01 11:30", periods=6, freq="10min")
    raw = pd.DataFrame({"time_local": times, "wind_ms": [1.0, 2, 3, 4, 5, 6]})

    hourly = centered_hourly(raw, "wind_ms")

    assert list(hourly.index) == [pd.Timestamp("2024-01-01 12:00")]
    assert hourly.iloc[0] == pytest.approx(3.5)


def test_temperature_check_counts_only_shift_towards_the_wind():
    rows = [{"group": f"Q{i}", "peak": 7.0} for i in range(5)]
    toward = temp_shift_check({"era5": [*rows, {"group": "disputed", "peak": 6.2}]}, {"disputed": -1})
    away = temp_shift_check({"era5": [*rows, {"group": "disputed", "peak": 7.8}]}, {"disputed": -1})

    assert toward[0]["moved"] is True
    assert away[0]["moved"] is False
