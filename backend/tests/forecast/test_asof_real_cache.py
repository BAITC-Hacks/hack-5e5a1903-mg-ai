"""``AsOfStore`` на закоммиченном кэше ``data/nwp``: то, что уйдет в ретро-симуляцию и в обучение."""

import pandas as pd
import pytest

from src.forecast.dataset.config import REPO
from src.forecast.weather.asof import AsOfStore
from src.forecast.weather.sources import SOURCES

ISSUES = pd.date_range("2026-01-31 02:00", "2026-02-27 02:00", freq="D", tz="UTC")


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def horizon(as_of: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(as_of + pd.Timedelta(hours=1), periods=48, freq="h")


@pytest.fixture(scope="module")
def store():
    return AsOfStore(REPO / "data" / "nwp")


def test_ifs_example_issue_2026_01_31(store):
    as_of = ts("2026-01-31 02:00")
    out = store.get_nwp("ifs", as_of, horizon(as_of))

    assert (out["run_init_utc"] == ts("2026-01-30 18:00")).all()
    assert (out["available_at_utc"] == ts("2026-01-31 01:30")).all()
    assert list(out["lead_h"]) == list(range(9, 57))


@pytest.mark.parametrize("as_of", ISSUES, ids=lambda t: f"{t:%m-%d}")
def test_february_issues_have_every_source_and_no_leak(store, as_of):
    out = store.get_nwp_multi(list(SOURCES), as_of, horizon(as_of))

    assert out.groupby("source").size().to_dict() == {name: 48 for name in SOURCES}
    assert (out["available_at_utc"] <= as_of).all()
    assert out[["ws80", "ws100", "ws120"]].notna().any(axis=1).all()


def test_empty_ifs_run_of_august_2025_is_not_used(store):
    as_of = ts("2025-08-05 02:00")
    out = store.get_nwp_multi(list(SOURCES), as_of, horizon(as_of))

    assert ts("2025-08-04 12:00") not in set(out.loc[out["source"] == "ifs", "run_init_utc"])
    assert out[["ws80", "ws100", "ws120"]].notna().any(axis=1).all()
    assert set(out["source"]) >= {"ifs025", "gfs", "icon", "gem"}
