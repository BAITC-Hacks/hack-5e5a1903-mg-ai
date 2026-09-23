"""HTTP-сервис погоды на синтетическом кэше и маленькой SCADA, без сети и без Postgres."""

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from src.forecast.weather.asof import AsOfStore
from src.forecast.weather.sources import SOURCES
from src.weather_service.catalog import ENSEMBLE_MEMBERS
from src.weather_service.main import create_app
from src.weather_service.service import WeatherData, hub_wind
from tests.forecast.synthetic_nwp import synthetic_cache, write_cache
from tests.forecast.test_scada import records, write_scada

START, END = "2026-01-10", "2026-02-06"
AS_OF = "2026-01-31T02:00:00Z"


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC")


def times(values) -> pd.Series:
    return pd.to_datetime(pd.Series(values), utc=True)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("data")
    for i, source in enumerate(SOURCES.values()):
        write_cache(synthetic_cache(source, START, END, seed=i), root / "nwp", source)
    # Сутки 30.01.2026 по времени SCADA (UTC+6): в UTC это 29.01 18:00 … 30.01 17:00.
    wind = list(np.linspace(5.0, 12.0, 144).round(2))
    power = list(np.linspace(0.1, 0.9, 144).round(3))
    write_scada(root, records("2026-01-30 00:00", n=144, wind=wind, power=power, temp=-5.0))
    return root


@pytest.fixture(scope="module")
def data(data_dir):
    return WeatherData.load(data_dir)


@pytest.fixture
async def client(data):
    async with AsyncClient(transport=ASGITransport(app=create_app(data)), base_url="http://weather") as c:
        yield c


def error_code(response) -> str:
    return response.json()["error"]["code"]


# ---------------------------------------------------------------- /health и /sources


async def test_health_reports_every_source_and_scada(client):
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body["sources"]) == set(SOURCES)
    assert all(info["runs"] > 0 for info in body["sources"].values())
    assert body["scada_rows"] == 48


async def test_health_is_degraded_without_scada(tmp_path, data):
    empty = WeatherData(AsOfStore(tmp_path / "nwp"), None)
    async with AsyncClient(transport=ASGITransport(app=create_app(empty)), base_url="http://weather") as c:
        body = (await c.get("/health")).json()
    assert body["status"] == "degraded"
    assert body["scada_rows"] == 0
    assert body["sources"]["gfs"] == {"runs": 0, "valid_from_utc": None, "valid_to_utc": None}


async def test_sources_describe_registry_and_ensemble(client):
    body = {item["source"]: item for item in (await client.get("/sources")).json()}

    assert list(body) == [*SOURCES, "ensemble"]
    assert body["ifs"]["kind"] == "single_runs" and body["ifs"]["publication_delay_h"] == 7.5 and body["ifs"]["delay_confirmed"]
    assert body["icon"]["delay_confirmed"] is False
    assert body["gem"]["run_step_h"] == 12
    # В синтетике у gfs и gem нет ветра на 100 м: это видно по кэшу.
    assert "ws100" in body["gem"]["empty_columns"] and 100 not in body["gem"]["wind_heights_m"]
    assert "ws10" in body["gfs"]["empty_columns"]
    ensemble = body["ensemble"]
    assert ensemble["kind"] == "ensemble"
    assert [m["source"] for m in ensemble["members"]] == list(ENSEMBLE_MEMBERS)
    assert sum(m["weight"] for m in ensemble["members"]) == pytest.approx(1.0)


# ---------------------------------------------------------------- /nwp


async def test_nwp_default_window_is_issue_horizon(client):
    response = await client.get("/nwp", params={"source": "ifs", "as_of": AS_OF})

    assert response.status_code == 200
    body = response.json()
    assert body["from_utc"] == "2026-01-31T03:00:00Z" and body["to_utc"] == "2026-02-02T02:00:00Z"
    rows = body["rows"]
    assert len(rows) == 48 and {row["source"] for row in rows} == {"ifs"}
    assert rows[0]["valid_time_utc"].endswith("Z")
    assert set(rows[0]) >= {"valid_time_utc", "source", "run_init_utc", "available_at_utc", "lead_h", "ws80", "wd100", "t2m", "ws10"}
    assert rows[0]["ws10"] is None


@pytest.mark.parametrize("as_of", ["2026-01-20T00:00:00Z", "2026-01-25T07:29:00Z", "2026-01-25T07:30:00Z", AS_OF, "2026-02-01T13:17:00Z"])
async def test_nwp_never_returns_rows_published_after_as_of(client, as_of):
    response = await client.get("/nwp", params={"source": "ifs,ifs025,gfs,icon,gem,ensemble", "as_of": as_of})

    assert response.status_code == 200
    rows = pd.DataFrame(response.json()["rows"])
    assert len(rows) == 48 * 6
    assert (times(rows["available_at_utc"]) <= ts(as_of)).all()
    assert (times(rows["run_init_utc"]) <= times(rows["available_at_utc"])).all()


async def test_nwp_matches_asof_store(client, data):
    body = (await client.get("/nwp", params={"source": "gfs", "as_of": AS_OF})).json()
    expected = data.store.get_nwp("gfs", ts(AS_OF), pd.date_range("2026-01-31 03:00", periods=48, freq="h", tz="UTC"))

    got = pd.DataFrame(body["rows"])
    assert list(times(got["run_init_utc"])) == list(expected["run_init_utc"])
    assert got["lead_h"].tolist() == expected["lead_h"].tolist()
    assert got["ws80"].tolist() == pytest.approx(expected["ws80"].tolist())


async def test_nwp_explicit_window(client):
    params = {"source": "gfs", "as_of": AS_OF, "from": "2026-01-31T05:00:00Z", "to": "2026-01-31T07:00:00Z"}
    rows = (await client.get("/nwp", params=params)).json()["rows"]
    assert [row["valid_time_utc"] for row in rows] == ["2026-01-31T05:00:00Z", "2026-01-31T06:00:00Z", "2026-01-31T07:00:00Z"]


async def test_nwp_no_run_available_for_single_source(client):
    response = await client.get("/nwp", params={"source": "ifs", "as_of": "2026-01-10T01:00:00Z"})

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "NO_RUN_AVAILABLE"
    assert error["details"]["source"] == "ifs"
    assert error["details"]["missing_hours"] == 48


async def test_nwp_skips_missing_source_when_several_requested(tmp_path):
    for name in ("gfs", "icon"):
        write_cache(synthetic_cache(SOURCES[name], START, END), tmp_path / "nwp", SOURCES[name])
    data = WeatherData(AsOfStore(tmp_path / "nwp"), None)
    async with AsyncClient(transport=ASGITransport(app=create_app(data)), base_url="http://weather") as c:
        body = (await c.get("/nwp", params={"source": "gfs,ifs,icon", "as_of": AS_OF})).json()
        only_missing = await c.get("/nwp", params={"source": "ifs,gem", "as_of": AS_OF})

    assert body["sources"] == ["gfs", "icon"]
    assert body["missing"] == [{"source": "ifs", "missing_hours": 48}]
    assert len(body["rows"]) == 96
    assert only_missing.status_code == 404 and error_code(only_missing) == "NO_RUN_AVAILABLE"


async def test_nwp_ensemble_is_mean_of_members_at_hub_height(client, data):
    body = (await client.get("/nwp", params={"source": "ensemble", "as_of": AS_OF})).json()
    rows = pd.DataFrame(body["rows"])
    hours = pd.date_range("2026-01-31 03:00", periods=48, freq="h", tz="UTC")
    members = data.store.get_nwp_multi(list(ENSEMBLE_MEMBERS), ts(AS_OF), hours)
    hub = members.assign(hub=hub_wind(members)).groupby("valid_time_utc")["hub"]

    assert len(rows) == 48 and set(rows["source"]) == {"ensemble"}
    assert rows["ws80"].tolist() == pytest.approx(hub.mean().round(2).tolist())
    assert rows["ws_spread"].tolist() == pytest.approx(hub.std(ddof=0).round(2).tolist())
    assert rows["members"].iloc[0] == sorted(ENSEMBLE_MEMBERS)
    assert list(times(rows["available_at_utc"])) == list(members.groupby("valid_time_utc")["available_at_utc"].max())
    assert rows["ws100"].isna().all() and rows["ws120"].isna().all()


async def test_nwp_unknown_source(client):
    response = await client.get("/nwp", params={"source": "ecmwf_ifs", "as_of": AS_OF})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "UNKNOWN_SOURCE"
    assert error["details"]["unknown"] == ["ecmwf_ifs"]
    assert "ensemble" in error["details"]["known"]


@pytest.mark.parametrize(
    "params",
    [
        {"source": "gfs", "as_of": "2026-01-31T02:00:00"},
        {"source": "gfs"},
        {"source": "gfs", "as_of": AS_OF, "from": "2026-01-31T03:30:00Z"},
        {"source": "gfs", "as_of": AS_OF, "from": "2026-01-31T03:00:00Z", "to": "2026-02-10T03:00:00Z"},
        {"source": "gfs", "as_of": AS_OF, "from": "2026-01-31T05:00:00Z", "to": "2026-01-31T03:00:00Z"},
    ],
    ids=["naive-time", "no-as-of", "not-on-hour", "window-too-big", "from-after-to"],
)
async def test_nwp_validation_errors(client, params):
    response = await client.get("/nwp", params=params)
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


async def test_nwp_other_offset_is_converted_to_utc(client):
    body = (await client.get("/nwp", params={"source": "gfs", "as_of": "2026-01-31T07:00:00+05:00"})).json()
    assert body["as_of_utc"] == AS_OF


async def test_leakage_guard_stops_response(data_dir):
    data = WeatherData.load(data_dir)
    frame = data.store.cache("ifs")
    # Ломаем выбор прогона: доступность в кэше уезжает на сутки позже, а индекс выбора остается прежним.
    data.store._data["ifs"] = data.store._data["ifs"].__class__(
        frame.assign(available_at_utc=frame["available_at_utc"] + pd.Timedelta(days=1)),
        data.store._data["ifs"].valid_ns,
        data.store._data["ifs"].available_ns,
        data.store._data["ifs"].runs,
    )
    async with AsyncClient(transport=ASGITransport(app=create_app(data)), base_url="http://weather") as c:
        response = await c.get("/nwp", params={"source": "ifs", "as_of": AS_OF})

    assert response.status_code == 500
    assert error_code(response) == "LEAKAGE_GUARD"


# ---------------------------------------------------------------- /runs


async def test_runs_statuses_relative_to_as_of(client, data):
    runs = pd.DataFrame((await client.get("/runs", params={"source": "gfs", "as_of": AS_OF})).json())
    hours = pd.date_range("2026-01-31 03:00", periods=48, freq="h", tz="UTC")
    chosen = data.store.get_nwp("gfs", ts(AS_OF), hours)

    assert set(runs["status"]) == {"used", "stale", "after_as_of"}
    used = runs[runs["status"] == "used"]
    assert set(times(used["run_init_utc"])) == set(chosen["run_init_utc"])
    assert used["hours_used"].sum() == 48
    assert (times(runs.loc[runs["status"] == "after_as_of", "available_at_utc"]) > ts(AS_OF)).all()
    assert (times(runs.loc[runs["status"] != "after_as_of", "available_at_utc"]) <= ts(AS_OF)).all()
    assert (runs.loc[runs["status"] != "used", "hours_used"] == 0).all()
    assert (runs["lead_from_h"] <= runs["lead_to_h"]).all()
    assert list(times(runs["available_at_utc"])) == sorted(times(runs["available_at_utc"]))


async def test_runs_status_filter_and_ensemble_expansion(client):
    runs = (await client.get("/runs", params={"source": "ensemble", "as_of": AS_OF, "status": "used"})).json()
    assert {run["source"] for run in runs} == set(ENSEMBLE_MEMBERS)
    assert {run["status"] for run in runs} == {"used"}


async def test_runs_recompute_keeps_issue_horizon(client):
    params = {"source": "ifs", "as_of": "2026-01-31T07:30:00Z", "issue_time": AS_OF, "status": "used"}
    runs = (await client.get("/runs", params=params)).json()
    assert [run["run_init_utc"] for run in runs] == ["2026-01-31T00:00:00Z"]


async def test_runs_events_mode_matches_run_events(client, data):
    params = {"source": "ifs,gfs", "from": AS_OF, "to": "2026-02-01T02:00:00Z"}
    runs = (await client.get("/runs", params=params)).json()
    expected = data.store.run_events(ts(AS_OF), ts("2026-02-01T02:00:00Z"), ["ifs", "gfs"])

    assert [(r["source"], ts(r["run_init_utc"])) for r in runs] == [(e.source, e.run_init_utc) for e in expected]
    assert all(r["status"] is None for r in runs)


@pytest.mark.parametrize(
    "params",
    [{}, {"from": AS_OF}, {"from": AS_OF, "to": "2026-02-01T02:00:00Z", "status": "used"}, {"as_of": AS_OF, "status": "fresh"}],
    ids=["nothing", "only-from", "status-without-as-of", "unknown-status"],
)
async def test_runs_validation_errors(client, params):
    response = await client.get("/runs", params=params)
    assert response.status_code == 422
    assert error_code(response) == "VALIDATION_ERROR"


# ---------------------------------------------------------------- /scada


async def test_scada_window_and_turbine(client):
    params = {"from": "2026-01-29T18:00:00Z", "until": "2026-01-29T21:00:00Z", "turbine": "T1"}
    rows = (await client.get("/scada", params=params)).json()

    assert [row["time_utc"] for row in rows] == ["2026-01-29T18:00:00Z", "2026-01-29T19:00:00Z", "2026-01-29T20:00:00Z"]
    assert {row["turbine"] for row in rows} == {"T1"}
    assert set(rows[0]) == {"time_utc", "turbine", "power_norm", "wind_ms", "temp_c", "flag"}


async def test_scada_after_history_is_empty(client):
    rows = (await client.get("/scada", params={"from": "2026-02-01T00:00:00Z", "until": "2026-02-02T00:00:00Z"})).json()
    assert rows == []


async def test_scada_window_limit(client):
    response = await client.get("/scada", params={"from": "2026-01-01T00:00:00Z", "until": "2026-03-01T00:00:00Z"})
    assert response.status_code == 422 and error_code(response) == "VALIDATION_ERROR"


async def test_scada_unavailable(tmp_path):
    data = WeatherData(AsOfStore(tmp_path), None)
    async with AsyncClient(transport=ASGITransport(app=create_app(data)), base_url="http://weather") as c:
        response = await c.get("/scada", params={"from": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"})
    assert response.status_code == 503 and error_code(response) == "DATA_UNAVAILABLE"
