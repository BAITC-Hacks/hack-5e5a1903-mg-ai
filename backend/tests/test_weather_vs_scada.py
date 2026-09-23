"""Тесты диагностики «погода из Open-Meteo против SCADA». Сеть не нужна."""

import numpy as np
import pandas as pd

from src.analysis.weather_vs_scada import (
    lag_correlations,
    load_scada_hourly,
    md_table,
    prev_day_run_init,
    to_utc,
)


def _write_scada(path, times: list[str], wind: list[float]) -> None:
    rows = [f"{i},{t},{w},0.5,10.0" for i, (t, w) in enumerate(zip(times, wind, strict=True), start=1)]
    header = "ID,Статистическое время,Средняя скорость ветра(m/s),Нормализованная активная мощность,Средняя температура окружающей среды(°C)"
    path.write_text("\n".join([header, *rows]), encoding="utf-8")


def test_hourly_mean_is_centered_on_the_hour(tmp_path):
    # Записи 00:30…01:20 попадают в час 01:00, записи 01:30 и 01:40 — в 02:00.
    times = [f"2024-05-01 {h}:{m:02d}:00" for h, m in [(0, 30), (0, 40), (0, 50), (1, 0), (1, 10), (1, 20), (1, 30), (1, 40)]]
    wind = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 100.0, 100.0]
    path = tmp_path / "scada.csv"
    _write_scada(path, times, wind)

    hourly = load_scada_hourly(path)

    assert list(hourly.index) == [pd.Timestamp("2024-05-01 01:00")]
    assert hourly.loc["2024-05-01 01:00", "wind"] == 3.5


def test_hour_with_too_few_records_is_dropped(tmp_path):
    times = ["2024-05-01 0:50:00", "2024-05-01 1:00:00", "2024-05-01 1:10:00"]
    path = tmp_path / "scada.csv"
    _write_scada(path, times, [1.0, 2.0, 3.0])

    assert load_scada_hourly(path).empty


def test_lag_scan_finds_known_offset():
    rng = np.random.default_rng(0)
    utc_index = pd.date_range("2025-01-01", periods=2000, freq="h")
    utc = pd.Series(np.cumsum(rng.normal(size=len(utc_index))), index=utc_index)
    # Локальное время SCADA = UTC + 6 ч.
    local = pd.Series(utc.to_numpy(), index=utc_index + pd.Timedelta(hours=6))

    corrs = lag_correlations(local, utc, range(0, 10))

    assert corrs.idxmax() == 6
    assert corrs[6] > 0.999


def test_to_utc_applies_offset_by_period():
    local = pd.DataFrame({"wind": [1.0, 2.0]}, index=pd.to_datetime(["2024-02-29 12:00", "2024-03-02 12:00"]))

    out = to_utc(local, offset_before=6, offset_after=5)

    assert list(out.index) == [pd.Timestamp("2024-02-29 06:00"), pd.Timestamp("2024-03-02 07:00")]


def test_prev_day_run_init_six_hour_cycle():
    # Проверено по Single Runs API: previous_day1 в 15:00 — прогон 12z предыдущих суток.
    t = pd.Timestamp("2026-08-04 15:00")
    assert prev_day_run_init(t, 1, 6) == pd.Timestamp("2026-08-03 12:00")
    assert prev_day_run_init(t, 2, 6) == pd.Timestamp("2026-08-02 12:00")
    assert prev_day_run_init(pd.Timestamp("2026-08-04 12:00"), 1, 6) == pd.Timestamp("2026-08-03 12:00")


def test_prev_day_run_init_twelve_hour_cycle_is_not_newer_than_six_hour():
    t = pd.Timestamp("2026-08-04 11:00")
    assert prev_day_run_init(t, 1, 12) == pd.Timestamp("2026-08-03 00:00")
    assert prev_day_run_init(t, 1, 12) <= prev_day_run_init(t, 1, 6)


def test_md_table_formats_numbers_and_missing():
    table = md_table([{"a": "x", "b": 1.234}, {"a": "y", "b": float("nan")}], [("a", "A"), ("b", "B")])

    assert table.splitlines() == ["| A | B |", "|---|---|", "| x | 1.23 |", "| y | — |"]
