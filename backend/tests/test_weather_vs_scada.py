"""Тесты диагностики «погода из Open-Meteo против SCADA». Сеть не нужна."""

import numpy as np
import pandas as pd
import pytest

import src.analysis.weather_vs_scada as wvs
from src.analysis.weather_vs_scada import (
    SKILL_HEIGHT,
    SPLIT,
    _cached,
    availability_section,
    lag_correlations,
    lead_label,
    linear_fit,
    load_scada_hourly,
    md_table,
    model_levels,
    prev_day_run_init,
    quarters_with_temp_lag,
    skill_section,
    to_utc,
    write_report,
)

# Смещения моделей разного знака, как у настоящих: IFS и GFS завышают, ICON и GEM занижают.
BIAS = {"ecmwf_ifs025": 0.6, "gfs_global": 0.8, "icon_global": -0.5, "gem_global": -1.6}


def _synthetic_forecasts(seed: int = 0) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Ветер SCADA и прогнозы четырех моделей = ветер + смещение модели + независимый шум."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(SPLIT - pd.Timedelta(days=120), SPLIT + pd.Timedelta(days=60), freq="h")
    obs = pd.Series(7.0 + 3.0 * rng.standard_normal(len(index)), index=index)
    nwp = {}
    for model, bias in BIAS.items():
        h = SKILL_HEIGHT[model]
        nwp[model] = pd.DataFrame(
            {f"wind_speed_{h}m_previous_day{n}": obs + bias + 2.0 * rng.standard_normal(len(index)) for n in (1, 2)},
            index=index,
        )
    return pd.DataFrame({"wind": obs}), nwp


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


def test_ensemble_mos_is_fit_on_the_ensemble_mean():
    site_utc, nwp = _synthetic_forecasts()
    obs = site_utc["wind"]
    train, test = obs[obs.index < SPLIT], obs[obs.index >= SPLIT]
    preds = {m: frame[f"wind_speed_{SKILL_HEIGHT[m]}m_previous_day1"] for m, frame in nwp.items()}
    mean = pd.concat(preds, axis=1).mean(axis=1)

    _, summary = skill_section(site_utc, nwp)

    a, b = linear_fit(train, mean[train.index])
    expected = (a + b * mean[test.index] - test).abs().mean()
    assert summary[1]["ens_mos_mae"] == pytest.approx(expected)
    # Прежний способ — среднее четырех поправленных прогонов — сжимает прогноз дважды и ошибается сильнее.
    fits = {m: linear_fit(train, p[train.index]) for m, p in preds.items()}
    mean_of_mos = pd.concat({m: fits[m][0] + fits[m][1] * p for m, p in preds.items()}, axis=1).mean(axis=1)
    assert summary[1]["ens_mos_mae"] < (mean_of_mos[test.index] - test).abs().mean()
    assert summary[1]["ens_mos_mae"] < summary[1]["best_mos_mae"]


def test_skill_summary_reports_gain_after_correction_for_both_leads():
    site_utc, nwp = _synthetic_forecasts()

    text, summary = skill_section(site_utc, nwp)

    assert set(summary) == {1, 2}
    for s in summary.values():
        assert {"best_mos", "best_mos_mae", "ens_mos_mae", "spread_grows"} <= set(s)
    assert "после поправки у обоих" in text


def test_ensemble_lead_label_covers_twelve_hour_gem_cycle():
    assert lead_label(1, 6) == "+24…29 ч"
    assert lead_label(1, 12) == "+24…35 ч"
    assert lead_label(2, 12) == "+48…59 ч"


def test_model_levels_are_relative_to_the_mean_of_four():
    index = pd.date_range("2025-01-01", periods=3, freq="h")
    winds = {"ecmwf_ifs025": 12.0, "gfs_global": 10.0, "icon_global": 10.0, "gem_global": 8.0}
    nwp = {m: pd.DataFrame({f"wind_speed_{SKILL_HEIGHT[m]}m": [v] * 3}, index=index) for m, v in winds.items()}

    levels = model_levels(nwp)

    assert levels == pytest.approx({"ecmwf_ifs025": 20.0, "gfs_global": 0.0, "icon_global": 0.0, "gem_global": -20.0})


def test_temperature_lag_counts_only_quarters_exactly_one_hour_later():
    # (ветер, температура): +1 ч, тот же час, +3 ч, +1 ч.
    assert quarters_with_temp_lag([(6, 7), (6, 6), (5, 8), (6, 7)]) == 2


def test_availability_summary_reports_first_dates_and_gaps():
    index = pd.date_range("2024-02-15", periods=96, freq="h")
    values = np.r_[[np.nan] * 24, np.ones(72)]
    nwp = {m: pd.DataFrame({f"wind_speed_{SKILL_HEIGHT[m]}m_previous_day{n}": values for n in (1, 2)}, index=index) for m in SKILL_HEIGHT}
    nwp["gem_global"].iloc[50, 0] = np.nan

    text, summary = availability_section(nwp)

    assert summary["all_present"]
    assert summary["first_min"] == summary["first_max"] == pd.Timestamp("2024-02-16")
    assert summary["missing_max"] == pytest.approx(100 / 72)
    assert "Совпадает со свежим прогоном" not in text


def test_refresh_returns_the_same_rounded_values_as_the_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(wvs, "CACHE_DIR", tmp_path)

    def fetch() -> pd.DataFrame:
        return pd.DataFrame({"x": [1.234567]}, index=pd.DatetimeIndex(["2025-01-01"], name="time"))

    refreshed = _cached("probe", True, fetch)
    from_cache = _cached("probe", False, fetch)

    pd.testing.assert_frame_equal(refreshed, from_cache)
    assert refreshed["x"].iloc[0] == 1.23


def test_report_is_written_with_lf_line_endings(tmp_path):
    path = tmp_path / "reports" / "report.md"

    write_report("# title\n\nline\n", path)

    assert path.read_bytes() == b"# title\n\nline\n"
