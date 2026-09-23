"""Тесты агента: выбор источника, флаги риска и пересчет по новому прогону."""

from __future__ import annotations

import pandas as pd
import pytest

from src.forecast.agent import (
    FLAG_CUT_OUT,
    FLAG_DEGRADED,
    FLAG_ICING,
    FLAG_RAMP,
    SOURCE_CLIMATOLOGY,
    STEPS,
    replay,
    run_issue,
)
from src.forecast.agent.decisions import (
    REASON_FALLBACK,
    REASON_NO_MATERIAL_CHANGE,
    REASON_NO_SOURCE,
    REASON_PUBLISH_NEW_VERSION,
)
from src.forecast.config import HORIZON_HOURS, LOCAL_UTC_OFFSET, RATED_MW_PER_TURBINE, SOURCE_GFS, SOURCE_IFS, TURBINES
from src.forecast.contract import FORECAST_COLUMNS, LeakageError
from tests.forecast.fakes import FakeModel, FakeRun, providers_for

ISSUE_TIME = pd.Timestamp("2026-02-01 02:00", tz="UTC")


def ifs_run(wind=8.0, temp_c=5.0, humidity_pct=50.0, init="2026-01-31 18:00"):
    """Прогон IFS, опубликованный за 30 минут до выпуска."""
    run_init = pd.Timestamp(init, tz="UTC")
    return FakeRun(SOURCE_IFS, run_init, run_init + pd.Timedelta(hours=7, minutes=30), wind, temp_c, humidity_pct)


def gfs_run(wind=8.0, init="2026-01-31 18:00"):
    run_init = pd.Timestamp(init, tz="UTC")
    return FakeRun(SOURCE_GFS, run_init, run_init + pd.Timedelta(hours=7), wind)


def flags_of(forecast: pd.DataFrame) -> set[str]:
    return {flag for row in forecast["flags"] for flag in row.split("|") if flag}


def test_forecast_has_48_hours_for_each_turbine():
    providers, _ = providers_for([ifs_run()])

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert list(result.forecast.columns) == list(FORECAST_COLUMNS)
    assert len(result.forecast) == HORIZON_HOURS * len(TURBINES)
    assert sorted(result.forecast["lead_h"].unique()) == list(range(1, HORIZON_HOURS + 1))
    assert set(result.forecast["turbine"]) == {turbine.name for turbine in TURBINES}

    quantiles = result.forecast[["p10", "p50", "p90"]]
    assert (quantiles["p10"] <= quantiles["p50"]).all()
    assert (quantiles["p50"] <= quantiles["p90"]).all()
    assert quantiles.to_numpy().min() >= 0.0
    assert quantiles.to_numpy().max() <= 1.0
    assert result.forecast["p50_mw"].max() <= RATED_MW_PER_TURBINE

    local = pd.to_datetime(result.forecast["valid_time_local"])
    utc = pd.to_datetime(result.forecast["valid_time_utc"], utc=True)
    assert ((utc + LOCAL_UTC_OFFSET).dt.tz_localize(None) == local).all()


def test_fallback_to_gfs_when_ifs_run_is_missing():
    providers, _ = providers_for([gfs_run()])

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert set(result.forecast["source"]) == {SOURCE_GFS}
    assert FLAG_DEGRADED in flags_of(result.forecast)
    reasons = [decision["reason_code"] for decision in result.decisions]
    assert REASON_FALLBACK in reasons
    fallback = next(d for d in result.decisions if d["reason_code"] == REASON_FALLBACK)
    assert fallback["inputs"]["source"] == SOURCE_IFS


def test_leakage_error_also_switches_to_the_backup_source():
    providers, weather = providers_for([ifs_run(), gfs_run()])
    original = weather.get_nwp

    def refuse_ifs(source, as_of, valid_times):
        if source == SOURCE_IFS:
            raise LeakageError("прогон опубликован позже момента выпуска")
        return original(source, as_of, valid_times)

    providers = type(providers)(
        get_nwp=refuse_ifs,
        run_events=providers.run_events,
        build_manifest=providers.build_manifest,
        build_features=providers.build_features,
        baseline=providers.baseline,
    )

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert set(result.forecast["source"]) == {SOURCE_GFS}


def test_climatology_when_no_run_is_available_at_all():
    providers, _ = providers_for([])

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert set(result.forecast["source"]) == {SOURCE_CLIMATOLOGY}
    assert FLAG_DEGRADED in flags_of(result.forecast)
    assert REASON_NO_SOURCE in [decision["reason_code"] for decision in result.decisions]
    assert len(result.forecast) == HORIZON_HOURS * len(TURBINES)


def test_rejects_a_frozen_run_and_takes_the_next_source():
    frozen = FakeRun(SOURCE_IFS, pd.Timestamp("2026-01-31 18:00", tz="UTC"), pd.Timestamp("2026-02-01 01:30", tz="UTC"), wind=[7.0] * HORIZON_HOURS)
    providers, _ = providers_for([frozen, gfs_run()])

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert set(result.forecast["source"]) == {SOURCE_GFS}
    assert any("застыл" in decision["reason"] for decision in result.decisions)


@pytest.mark.parametrize(
    ("run", "flag"),
    [
        (ifs_run(wind=30.0), FLAG_CUT_OUT),
        (ifs_run(temp_c=0.0, humidity_pct=95.0), FLAG_ICING),
    ],
)
def test_weather_risk_flags(run, flag):
    providers, _ = providers_for([run])

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert flag in flags_of(result.forecast)


def test_ramp_flag_on_a_sharp_wind_rise():
    profile = [4.0] * 24 + [14.0] * 24
    providers, _ = providers_for(
        [FakeRun(SOURCE_IFS, pd.Timestamp("2026-01-31 18:00", tz="UTC"), pd.Timestamp("2026-02-01 01:30", tz="UTC"), wind=profile)]
    )

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert FLAG_RAMP in flags_of(result.forecast)


def test_quiet_weather_leaves_no_flags():
    providers, _ = providers_for([ifs_run(wind=8.0, temp_c=10.0, humidity_pct=40.0)])

    result = run_issue(ISSUE_TIME, FakeModel(), providers)

    assert flags_of(result.forecast) == set()


def _replay_one_day(runs, tmp_path):
    providers, _ = providers_for(runs)
    captured: list = []
    results = replay(
        "2026-02-01",
        "2026-02-01",
        tmp_path,
        providers,
        model=FakeModel(),
        sink=lambda items, output_dir: captured.extend(items),
    )
    assert captured == results
    return results


def test_new_version_is_published_when_the_forecast_changes_a_lot(tmp_path):
    later = ifs_run(wind=14.0, init="2026-02-01 00:00")

    results = _replay_one_day([ifs_run(wind=8.0), later], tmp_path)

    assert [result.version for result in results] == [1, 2]
    reasons = [decision["reason_code"] for decision in results[-1].decisions]
    assert REASON_PUBLISH_NEW_VERSION in reasons
    assert set(results[-1].forecast["version"]) == {2}


def test_no_new_version_when_the_change_is_immaterial(tmp_path):
    later = ifs_run(wind=8.02, init="2026-02-01 00:00")

    results = _replay_one_day([ifs_run(wind=8.0), later], tmp_path)

    assert [result.version for result in results] == [1]
    reasons = [decision["reason_code"] for decision in results[0].decisions]
    assert REASON_NO_MATERIAL_CHANGE in reasons
    assert REASON_PUBLISH_NEW_VERSION not in reasons


def test_every_step_of_the_case_appears_in_the_journal(tmp_path):
    results = _replay_one_day([ifs_run(wind=8.0), ifs_run(wind=14.0, init="2026-02-01 00:00")], tmp_path)

    steps = {decision["step"] for decision in results[-1].decisions}
    assert steps == set(STEPS)


def test_replay_keeps_the_issue_time_while_knowledge_moves_forward(tmp_path):
    results = _replay_one_day([ifs_run(wind=8.0), ifs_run(wind=14.0, init="2026-02-01 00:00")], tmp_path)

    issue_times = {pd.Timestamp(result.issue_time_utc) for result in results}
    assert issue_times == {ISSUE_TIME}

    recompute = [decision for decision in results[-1].decisions if decision["step"] == "recompute_on_update"]
    assert recompute
    assert all(decision["as_of_utc"] > decision["issue_time_utc"] for decision in recompute)
