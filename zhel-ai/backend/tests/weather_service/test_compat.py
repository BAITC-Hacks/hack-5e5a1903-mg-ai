"""Сервис погоды отвечает так, как его читают backend dev1 и ML-сервис dev2.

Запросы повторяют ``HttpWeatherSource`` из backend: те же пути, те же параметры. Ответы проверяются
копиями схем соседей из ``consumer_schemas.py``: ``NwpRow`` у backend требует ``t2m`` в каждой строке,
``WeatherRow`` у ML-сервиса ограничивает диапазоны значений.
"""

import numpy as np
import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import TypeAdapter

from src.forecast.dataset import config
from src.forecast.weather.asof import AsOfStore
from src.forecast.weather.sources import SOURCES
from src.weather_service.main import create_app
from src.weather_service.service import WeatherData
from tests.forecast.synthetic_nwp import synthetic_cache, write_cache
from tests.weather_service.consumer_schemas import UPSTREAM_NO_RUN, NwpResponse, RunRow, WeatherRow

START, END = "2026-01-10", "2026-02-06"
ISSUE = "2026-01-31T02:00:00Z"
HORIZON = {"from": "2026-01-31T03:00:00Z", "to": "2026-02-02T02:00:00Z"}
# Окно пересчета у backend: сутки после выпуска.
RECOMPUTE_TO = "2026-02-01T02:00:00Z"
# У IFS прогон 18z за 30.01 доступен в 01:30, раньше выпуска: без температуры его брать нельзя.
IFS_WITHOUT_T2M = pd.Timestamp("2026-01-30 18:00", tz="UTC")

WEATHER_ROWS = TypeAdapter(list[WeatherRow])
RUN_ROWS = TypeAdapter(list[RunRow])


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value).tz_convert("UTC")


def realistic(frame: pd.DataFrame) -> pd.DataFrame:
    """Синтетика N(8, 3) в правдоподобных единицах: иначе давление в 8 гПа не пройдет ``WeatherRow``."""
    out = frame.copy()
    for column in ("wind_speed_80m", "wind_speed_100m", "wind_speed_120m", "wind_gusts_10m"):
        out[column] = out[column].abs()
    out["wind_direction_100m"] = (out["wind_direction_100m"] * 20) % 360
    out["temperature_2m"] = out["temperature_2m"] - 13
    out["relative_humidity_2m"] = (out["relative_humidity_2m"] * 8).clip(0, 100)
    out["surface_pressure"] = out["surface_pressure"] + 942
    return out


def without_t2m(frame: pd.DataFrame, runs) -> pd.DataFrame:
    out = frame.copy()
    out.loc[out["run_init_utc"].isin(list(runs)), "temperature_2m"] = np.nan
    return out


async def serve(data: WeatherData):
    return AsyncClient(transport=ASGITransport(app=create_app(data)), base_url="http://weather")


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    root = tmp_path_factory.mktemp("compat")
    for i, source in enumerate(SOURCES.values()):
        frame = realistic(synthetic_cache(source, START, END, seed=i))
        if source.name == "ifs":
            frame = without_t2m(frame, [IFS_WITHOUT_T2M])
        write_cache(frame, root / "nwp", source)
    return WeatherData(AsOfStore(root / "nwp"), None)


@pytest.fixture
async def client(data):
    async with await serve(data) as c:
        yield c


@pytest.mark.parametrize("source", [*SOURCES, "ensemble"])
async def test_nwp_answer_fits_backend_client_and_ml_rows(client, source):
    response = await client.get("/nwp", params={"source": source, "as_of": ISSUE, **HORIZON})

    assert response.status_code == 200
    rows = NwpResponse.model_validate(response.json()).rows
    WEATHER_ROWS.validate_python(response.json()["rows"])
    assert len(rows) == 48
    assert all(row.available_at_utc <= ts(ISSUE) for row in rows)


async def test_hour_without_temperature_takes_older_run(client):
    rows = (await client.get("/nwp", params={"source": "ifs", "as_of": ISSUE, **HORIZON})).json()["rows"]

    assert {row["run_init_utc"] for row in rows} == {"2026-01-30T12:00:00Z"}
    assert all(row["t2m"] is not None for row in rows)


async def test_runs_status_agrees_with_temperature_fallback(client):
    runs = (await client.get("/runs", params={"source": "ifs", "as_of": ISSUE})).json()
    status = {run["run_init_utc"]: run["status"] for run in runs}

    assert status["2026-01-30T18:00:00Z"] == "stale"
    assert status["2026-01-30T12:00:00Z"] == "used"


async def test_no_run_without_temperature_is_reported(tmp_path):
    source = SOURCES["ifs"]
    frame = realistic(synthetic_cache(source, START, END))
    write_cache(without_t2m(frame, frame["run_init_utc"].unique()), tmp_path / "nwp", source)

    async with await serve(WeatherData(AsOfStore(tmp_path / "nwp"), None)) as c:
        response = await c.get("/nwp", params={"source": "ifs", "as_of": ISSUE, **HORIZON})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == UPSTREAM_NO_RUN


async def test_no_run_code_is_what_backend_expects(client):
    response = await client.get("/nwp", params={"source": "ifs", "as_of": "2026-01-10T01:00:00Z"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == UPSTREAM_NO_RUN


async def test_runs_answer_fits_backend_client(client):
    """Backend спрашивает события пересчета с ``as_of``, ``from`` и ``to`` одновременно."""
    response = await client.get("/runs", params={"as_of": ISSUE, "from": ISSUE, "to": RECOMPUTE_TO})

    assert response.status_code == 200
    runs = RUN_ROWS.validate_python(response.json())
    assert runs
    assert all(ts(ISSUE) < run.available_at_utc <= ts(RECOMPUTE_TO) for run in runs)


@pytest.mark.skipif(not (config.DATA_DIR / "nwp").is_dir(), reason="нет закоммиченного кэша data/nwp")
async def test_february_issues_on_committed_cache_fit_consumers():
    """28 выпусков февраля на настоящем кэше: либо 48 строк по контракту соседей, либо штатный ``NO_RUN_AVAILABLE``."""
    data = WeatherData(AsOfStore(config.DATA_DIR / "nwp"), None)
    issues = pd.date_range("2026-01-31 02:00", "2026-02-27 02:00", freq="D", tz="UTC")
    served = 0
    async with await serve(data) as c:
        for issue in issues:
            for source in [*SOURCES, "ensemble"]:
                response = await c.get("/nwp", params={"source": source, "as_of": issue.strftime("%Y-%m-%dT%H:%M:%SZ")})
                if response.status_code == 404:
                    assert response.json()["error"]["code"] == UPSTREAM_NO_RUN
                    continue
                assert response.status_code == 200, (issue, source, response.text)
                rows = NwpResponse.model_validate(response.json()).rows
                WEATHER_ROWS.validate_python(response.json()["rows"])
                assert len(rows) == 48 and all(row.available_at_utc <= issue for row in rows)
                served += 1
    assert served == len(issues) * (len(SOURCES) + 1)
