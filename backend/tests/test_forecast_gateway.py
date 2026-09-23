"""Граница с соседями: живой выпуск, отказы и переход на заглушку.

Настоящие сервисы не поднимаются: погода подменяется поддельным источником,
ML-сервис — транспортом ``httpx.MockTransport``. Проверяется то, ради чего
граница и написана: живой путь, таймаут, недоступность, мусор в ответе
и то, что при любом отказе пользователь получает ответ, а не пустой экран.
"""

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import AsyncClient

from src.core.security import create_access_token
from src.modules.auth.models import User
from src.modules.forecast import analyze, orchestrator
from src.modules.forecast.clients.base import UpstreamError
from src.modules.forecast.clients.ml import MlClient
from src.modules.forecast.clients.schemas import NwpRow, RunRow
from src.modules.forecast.clients.weather import (
    CODE_LEAKAGE,
    HttpWeatherSource,
    LocalWeatherSource,
    WeatherGateway,
    WeatherLeakageError,
    no_run_error,
)
from src.modules.forecast.config import DEFAULT_SOURCES, HORIZON_HOURS, TURBINES
from src.modules.forecast.service import FIRST_ISSUE

ISSUE = FIRST_ISSUE
ISSUE_PATH = ISSUE.isoformat()
ISSUE_TIME = datetime(ISSUE.year, ISSUE.month, ISSUE.day, 2, tzinfo=UTC)
TURBINE_NAMES = [turbine.name for turbine in TURBINES]


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


# --- поддельные соседи ---------------------------------------------------


def nwp_rows(
    source: str = "ifs025",
    *,
    hours: int = HORIZON_HOURS,
    wind: float = 5.0,
    drift: float = 0.1,
    temp: float = -5.0,
    humidity: float = 60.0,
    available_at: datetime | None = None,
) -> list[NwpRow]:
    """Прогон на горизонт выпуска.

    Ветер по умолчанию слегка растет: постоянный ветер на всем горизонте агент
    считает «застывшим» прогоном и отбраковывает, это отдельный тест.
    """
    run_init = ISSUE_TIME - timedelta(hours=8)
    rows = []
    for lead in range(1, hours + 1):
        rows.append(
            NwpRow(
                valid_time_utc=ISSUE_TIME + timedelta(hours=lead),
                source=source,
                run_init_utc=run_init,
                available_at_utc=available_at or run_init + timedelta(hours=7),
                lead_h=lead + 8,
                t2m=temp,
                ws80=round(wind + lead * drift, 2),
                rh2m=humidity,
            )
        )
    return rows


class FakeWeather:
    """Источник погоды, который отвечает ровно тем, что положили в тест."""

    kind = "fake"

    def __init__(self, by_source: dict[str, list[NwpRow]] | None = None, runs: list[RunRow] | None = None, error: Exception | None = None):
        self.by_source = by_source if by_source is not None else {"ifs025": nwp_rows()}
        self._runs = runs or []
        self._error = error
        self.asked: list[tuple[str, datetime]] = []

    async def nwp(self, *, source: str, as_of: datetime, valid_times) -> list[NwpRow]:
        self.asked.append((source, as_of))
        if self._error is not None:
            raise self._error
        if source not in self.by_source:
            raise no_run_error(source, as_of, "в тесте этого источника нет")
        return self.by_source[source]

    async def runs(self, *, time_from: datetime, time_to: datetime, sources=None) -> list[RunRow]:
        return [row for row in self._runs if time_from < row.available_at_utc <= time_to]


def model_info_payload(*, sources: list[str] | None = None, trained: bool = False) -> dict:
    return {
        "name": "baseline-power-curve" if not trained else "lightgbm-quantile",
        "version": "v1",
        "kind": "baseline_power_curve" if not trained else "lightgbm_quantile",
        "quantiles": [0.1, 0.5, 0.9],
        "trained_until_utc": None if not trained else "2026-01-31T00:00:00Z",
        "train_rows": None if not trained else 25000,
        "walk_forward": "обучение до 31.01.2026",
        "turbines": TURBINE_NAMES,
        "capacity_mw": {"T1": 2.5, "T2": 2.5},
        "inputs": {"sources": [{"name": name, "required": True} for name in sources or []], "variables": ["ws80"]},
        "features": [{"name": "ws80", "importance": 1.0}],
        "power_curve": [{"wind_ms": 0.0, "power_norm": 0.0}, {"wind_ms": 12.0, "power_norm": 1.0}],
    }


def predict_payload(request: httpx.Request, *, warnings: list[dict] | None = None, degraded: bool = False, shift: float = 0.0) -> dict:
    """Ответ модели, построенный по тем часам, которые агент действительно прислал."""
    body = json.loads(request.content)
    moments = sorted({row["valid_time_utc"] for row in body["rows"]})
    forecast = []
    for index, moment in enumerate(moments):
        p50 = min(1.0, round(0.30 + index * 0.005 + shift, 4))
        for turbine in body["options"]["turbines"]:
            forecast.append(
                {
                    "valid_time_utc": moment,
                    "lead_h": index + 1,
                    "turbine": turbine,
                    "p10": round(max(0.0, p50 - 0.07), 4),
                    "p50": p50,
                    "p90": round(min(1.0, p50 + 0.07), 4),
                }
            )
    return {
        "request_id": body.get("request_id"),
        "issue_time_utc": body["issue_time_utc"],
        "model": {"name": "baseline-power-curve", "version": "v1", "kind": "baseline_power_curve"},
        "unit": "capacity_fraction",
        "capacity_mw": {"T1": 2.5, "T2": 2.5},
        "forecast": forecast,
        "hourly_inputs": [],
        "warnings": warnings or [],
        "degraded": degraded,
        "inference_ms": 1.0,
    }


def ml_transport(
    *,
    predict=None,
    model_info: dict | None = None,
    metrics: dict | None = None,
    metrics_status: int = 200,
) -> httpx.MockTransport:
    """ML-сервис на подделанном транспорте: ни сети, ни контейнера."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/model-info":
            return httpx.Response(200, json=model_info if model_info is not None else model_info_payload(sources=["ifs025"]))
        if request.url.path == "/metrics":
            if metrics_status != 200:
                return httpx.Response(metrics_status, json={"error": {"code": "METRICS_NOT_AVAILABLE", "message": "нет метрик", "details": {}}})
            return httpx.Response(200, json=metrics)
        if request.url.path == "/predict":
            if callable(predict):
                return predict(request)
            return httpx.Response(200, json=predict_payload(request))
        return httpx.Response(404, json={"error": {"code": "NOT_FOUND", "message": "нет такого пути", "details": {}}})

    return httpx.MockTransport(handle)


@pytest.fixture
def live(monkeypatch):
    """Оба соседа отвечают по контракту."""

    def setup(weather: FakeWeather | None = None, **transport) -> FakeWeather:
        source = weather or FakeWeather()
        monkeypatch.setattr(orchestrator, "weather_source", lambda: source)
        monkeypatch.setattr(orchestrator, "ml_client", lambda: MlClient(base_url="http://ml", transport=ml_transport(**transport)))
        return source

    return setup


# --- живой путь ----------------------------------------------------------


async def test_live_forecast_is_built_from_both_neighbours(client: AsyncClient, user: User, live):
    live()

    response = await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["data_source"] == "live"
    assert len(body["hours"]) == HORIZON_HOURS * len(TURBINES)
    assert {hour["turbine"] for hour in body["hours"]} == set(TURBINE_NAMES)
    assert all(hour["p10"] <= hour["p50"] <= hour["p90"] for hour in body["hours"])
    assert body["issue"]["source"] == "ifs025"
    assert body["issue"]["degraded"] is False


async def test_live_megawatts_come_from_the_capacity_of_the_model(client: AsyncClient, user: User, live):
    live()

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()

    hour = body["hours"][0]
    assert hour["p50_mw"] == pytest.approx(hour["p50"] * 2.5)


async def test_agent_log_of_a_live_issue_walks_every_step(client: AsyncClient, user: User, live):
    live()

    response = await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))

    assert response.status_code == 200
    rows = response.json()
    assert {row["step"] for row in rows} == {"fetch_weather", "prepare", "run_model", "forecast", "analyze", "recompute_on_update"}
    assert rows[0]["step"] == "fetch_weather"
    assert rows[-1]["step"] == "recompute_on_update"
    assert all(row["reason_code"] for row in rows)


async def test_the_agent_asks_the_model_which_sources_it_was_trained_on(client: AsyncClient, user: User, live):
    weather = live(FakeWeather({"gfs": nwp_rows("gfs")}), model_info=model_info_payload(sources=["gfs"]))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()

    assert [source for source, _ in weather.asked] == ["gfs"]
    assert body["issue"]["source"] == "gfs"


async def test_without_a_model_card_the_agent_falls_back_to_the_default_sources(client: AsyncClient, user: User, live):
    weather = live(FakeWeather({name: nwp_rows(name) for name in DEFAULT_SOURCES}), model_info=model_info_payload())

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()

    assert [source for source, _ in weather.asked] == list(DEFAULT_SOURCES)
    assert body["issue"]["source"] == "+".join(DEFAULT_SOURCES)


async def test_a_missing_source_degrades_the_issue_instead_of_dropping_it(client: AsyncClient, user: User, live):
    live(FakeWeather({"ifs025": nwp_rows("ifs025")}), model_info=model_info_payload(sources=["ifs025", "gfs"]))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert body["data_source"] == "live"
    assert body["issue"]["degraded"] is True
    assert all(analyze.FLAG_DEGRADED in hour["flags"] for hour in body["hours"])
    assert "FALLBACK" in {row["reason_code"] for row in log}


async def test_sources_that_disagree_widen_the_interval(client: AsyncClient, user: User, live):
    seen: dict[str, float] = {}

    def predict(request: httpx.Request) -> httpx.Response:
        seen["scale"] = json.loads(request.content)["options"]["interval_scale"]
        return httpx.Response(200, json=predict_payload(request))

    live(
        FakeWeather({"ifs025": nwp_rows("ifs025", wind=4.0), "gfs": nwp_rows("gfs", wind=15.0)}),
        model_info=model_info_payload(sources=["ifs025", "gfs"]),
        predict=predict,
    )

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()

    # Ветер расходится на 11 м/с при пороге 5: шаг за каждый метр сверх порога
    # упирается в потолок, и интервал расширяется ровно на него.
    assert seen["scale"] == pytest.approx(orchestrator.WIDE_INTERVAL_SCALE_MAX)
    assert all(analyze.FLAG_SOURCE_SPREAD in hour["flags"] for hour in body["hours"])


async def test_interval_widening_grows_with_the_spread_and_stops_at_the_cap():
    def collected(spread):
        weather = orchestrator._Weather(rows=[], points={}, sources=[], flags={analyze.FLAG_SOURCE_SPREAD}, spread_ms=spread)
        return orchestrator._interval_scale(weather)

    assert orchestrator._interval_scale(orchestrator._Weather(rows=[], points={}, sources=[], flags=set(), spread_ms=9.0)) == 1.0
    assert collected(analyze.SOURCE_SPREAD_MS) == 1.0
    assert collected(7.0) < collected(12.0)
    assert collected(100.0) == orchestrator.WIDE_INTERVAL_SCALE_MAX


async def test_weather_published_after_the_issue_is_dropped(client: AsyncClient, user: User, live):
    leaked = nwp_rows("ifs025", hours=1, available_at=ISSUE_TIME + timedelta(hours=3))
    live(FakeWeather({"ifs025": nwp_rows("ifs025") + leaked}))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert len(body["hours"]) == HORIZON_HOURS * len(TURBINES)
    assert "LEAKAGE_DROPPED" in {row["reason_code"] for row in log}


async def test_a_model_warning_lands_in_the_journal(client: AsyncClient, user: User, live):
    warning = {"code": "BASELINE_MODEL", "message": "обученной модели нет, работает паспортная кривая", "source": None}
    live(predict=lambda request: httpx.Response(200, json=predict_payload(request, warnings=[warning], degraded=True)))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert body["data_source"] == "live"
    assert body["issue"]["degraded"] is True
    assert "BASELINE_MODEL" in {row["reason_code"] for row in log}


async def test_dispatch_follows_the_live_issue(client: AsyncClient, user: User, live):
    live()

    response = await client.get(f"/api/forecast/{ISSUE_PATH}/dispatch", params={"risk": 0.2}, headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["data_source"] == "live"
    assert len(body["hours"]) == 24


def backtest_series(actual: float) -> list[dict]:
    """Два часа бэктеста по обеим турбинам: P10 = 0,2, P50 = 0,4 номинала."""
    return [
        {
            "issue_time_utc": "2025-01-10T02:00:00Z",
            "valid_time_utc": f"2025-01-10T{hour}:00:00Z",
            "lead_h": hour - 2,
            "turbine": turbine,
            "p10": 0.2,
            "p50": 0.4,
            "p90": 0.7,
            "actual": actual,
        }
        for hour in (19, 20)
        for turbine in TURBINE_NAMES
    ]


async def test_dispatch_shortfall_comes_from_the_backtest(client: AsyncClient, user: User, live):
    live(metrics={**metrics_payload(), "series": backtest_series(actual=0.3)})

    careful = (await client.get(f"/api/forecast/{ISSUE_PATH}/dispatch", params={"risk": 0.1}, headers=auth(user))).json()["kpi"]
    bold = (await client.get(f"/api/forecast/{ISSUE_PATH}/dispatch", params={"risk": 0.5}, headers=auth(user))).json()["kpi"]

    assert careful["backtest_hours"] == bold["backtest_hours"] == 2
    assert careful["shortfall_hours_share"] == 0.0
    assert bold["shortfall_hours_share"] == 1.0
    assert bold["mean_shortfall_mwh"] == 12.0


async def test_dispatch_without_a_backtest_leaves_the_shortfall_empty(client: AsyncClient, user: User, live):
    live(metrics_status=404)

    response = await client.get(f"/api/forecast/{ISSUE_PATH}/dispatch", params={"risk": 0.2}, headers=auth(user))

    assert response.status_code == 200
    assert len(response.json()["hours"]) == 24
    kpi = response.json()["kpi"]
    assert kpi["shortfall_hours_share"] is None
    assert kpi["mean_shortfall_mwh"] is None
    assert kpi["backtest_hours"] == 0


# --- пересчет по новому прогону ------------------------------------------


def pending_run(hours_after: int = 5) -> RunRow:
    return RunRow(source="ifs025", run_init_utc=ISSUE_TIME, available_at_utc=ISSUE_TIME + timedelta(hours=hours_after))


async def test_recompute_publishes_a_new_version_when_the_forecast_moves(client: AsyncClient, user: User, live):
    passes = {"count": 0}

    def predict(request: httpx.Request) -> httpx.Response:
        passes["count"] += 1
        return httpx.Response(200, json=predict_payload(request, shift=0.0 if passes["count"] == 1 else 0.3))

    live(FakeWeather(runs=[pending_run()]), predict=predict)

    response = await client.post(f"/api/forecast/{ISSUE_PATH}/recompute", headers=auth(user))

    assert response.status_code == 200
    assert response.json()["issue"]["version"] == 2


async def test_recompute_keeps_the_version_when_nothing_changes(client: AsyncClient, user: User, live):
    live(FakeWeather(runs=[pending_run()]))

    response = await client.post(f"/api/forecast/{ISSUE_PATH}/recompute", headers=auth(user))

    assert response.json()["issue"]["version"] == 1


async def test_recompute_without_a_new_run_changes_nothing(client: AsyncClient, user: User, live):
    live(FakeWeather())

    response = await client.post(f"/api/forecast/{ISSUE_PATH}/recompute", headers=auth(user))

    assert response.json()["issue"]["version"] == 1


# --- отказы соседей ------------------------------------------------------


def ml_error_cases():
    return [
        pytest.param(httpx.TimeoutException("too slow"), "ML_TIMEOUT", id="timeout"),
        pytest.param(httpx.ConnectError("no route"), "ML_UNAVAILABLE", id="unavailable"),
    ]


@pytest.mark.parametrize(("failure", "code"), ml_error_cases())
async def test_a_broken_model_service_falls_back_to_the_stub(client: AsyncClient, user: User, live, failure, code):
    def predict(request: httpx.Request) -> httpx.Response:
        raise failure

    live(predict=predict)

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert body["data_source"] == "stub"
    assert len(body["hours"]) == HORIZON_HOURS * len(TURBINES)
    assert code in {row["reason_code"] for row in log}
    assert log[-1]["step"] == "recompute_on_update"


async def test_rubbish_from_the_model_service_falls_back_to_the_stub(client: AsyncClient, user: User, live):
    def predict(request: httpx.Request) -> httpx.Response:
        payload = predict_payload(request)
        payload["forecast"][0]["p10"] = 0.99  # p10 больше p90: такой ответ принимать нельзя
        return httpx.Response(200, json=payload)

    live(predict=predict)

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert body["data_source"] == "stub"
    assert "ML_BAD_RESPONSE" in {row["reason_code"] for row in log}


async def test_a_model_answer_that_is_not_json_falls_back_to_the_stub(client: AsyncClient, user: User, live):
    live(predict=lambda request: httpx.Response(200, content=b"<html>gateway error</html>"))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()

    assert body["data_source"] == "stub"


async def test_a_rejected_request_falls_back_to_the_stub(client: AsyncClient, user: User, live):
    rejection = {"error": {"code": "LEAKAGE_DETECTED", "message": "строки позже момента выпуска", "details": {}}}
    live(predict=lambda request: httpx.Response(422, json=rejection))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert body["data_source"] == "stub"
    assert "ML_REJECTED" in {row["reason_code"] for row in log}


async def test_an_incomplete_model_answer_falls_back_to_the_stub(client: AsyncClient, user: User, live):
    def predict(request: httpx.Request) -> httpx.Response:
        payload = predict_payload(request)
        payload["forecast"] = [point for point in payload["forecast"] if point["turbine"] == "T1"]
        return httpx.Response(200, json=payload)

    live(predict=predict)

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()

    assert body["data_source"] == "stub"


async def test_without_any_weather_the_issue_falls_back_to_the_stub(client: AsyncClient, user: User, live):
    live(FakeWeather({}))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert body["data_source"] == "stub"
    assert "WEATHER_NO_RUN" in {row["reason_code"] for row in log}
    assert log[0]["step"] == "fetch_weather"


async def test_a_frozen_run_is_rejected_and_the_issue_degrades(client: AsyncClient, user: User, live):
    live(FakeWeather({"ifs025": nwp_rows("ifs025", wind=7.0, drift=0.0)}))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()
    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()

    assert body["data_source"] == "stub"
    assert "VALIDATE_FAIL" in {row["reason_code"] for row in log}


async def test_an_unknown_day_is_still_an_error_and_not_a_stub(client: AsyncClient, user: User, live):
    live()

    response = await client.get("/api/forecast/2025-06-01", headers=auth(user))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ISSUE_NOT_FOUND"


async def test_leaked_weather_is_refused_instead_of_being_hidden(client: AsyncClient, user: User, live, monkeypatch):
    live(FakeWeather(error=WeatherLeakageError("прогон опубликован позже момента выпуска")))

    response = await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == CODE_LEAKAGE


# --- страницы модели и бэктеста ------------------------------------------


def metrics_payload() -> dict:
    return {
        "model_version": "v1",
        "period_start_utc": "2026-01-01T00:00:00Z",
        "period_end_utc": "2026-01-31T00:00:00Z",
        "nmae_d1_pct": 11.1,
        "nmae_d2_pct": 17.2,
        "nrmse_48_pct": 20.0,
        "skill_vs_persistence_pct": 60.0,
        "coverage_p10_p90_pct": 81.0,
        "baselines": [{"name": "Персистентность", "nmae_pct": 30.0}],
        "by_day": [{"issue_time_utc": "2026-01-05T02:00:00Z", "nmae_pct": 12.0, "bias_pct": -1.0}],
        "by_lead": [{"lead_h": 1, "nmae_pct": 9.0}],
    }


async def test_backtest_shows_live_metrics(client: AsyncClient, user: User, live):
    live(metrics=metrics_payload())

    response = await client.get("/api/forecast/backtest", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["data_source"] == "live"
    assert body["nmae_d1_pct"] == 11.1
    assert body["by_day"][0]["issue_date"] == "2026-01-05"


async def test_missing_metrics_are_shown_honestly(client: AsyncClient, user: User, live):
    live(metrics_status=404)

    response = await client.get("/api/forecast/backtest", headers=auth(user))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "METRICS_NOT_AVAILABLE"


async def test_a_trained_model_card_comes_from_the_service(client: AsyncClient, user: User, live):
    live(model_info=model_info_payload(trained=True))

    body = (await client.get("/api/forecast/model", headers=auth(user))).json()

    assert body["data_source"] == "live"
    assert body["trained_until"] == "2026-01-31"
    assert body["train_rows"] == 25000


async def test_an_untrained_model_card_stays_a_demo(client: AsyncClient, user: User, live):
    live()

    body = (await client.get("/api/forecast/model", headers=auth(user))).json()

    assert body["data_source"] == "stub"


# --- клиенты по отдельности ----------------------------------------------


async def test_the_http_weather_source_maps_failures_to_codes():
    cases = [
        (httpx.TimeoutException("slow"), "WEATHER_TIMEOUT"),
        (httpx.ConnectError("down"), "WEATHER_UNAVAILABLE"),
    ]
    for failure, code in cases:

        def handle(request: httpx.Request, failure=failure) -> httpx.Response:
            raise failure

        source = HttpWeatherSource(base_url="http://weather", transport=httpx.MockTransport(handle))
        with pytest.raises(UpstreamError) as raised:
            await source.nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=1)])
        assert raised.value.code == code


async def test_the_http_weather_source_rejects_an_answer_off_contract():
    source = HttpWeatherSource(
        base_url="http://weather", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"rows": [{"nonsense": 1}]}))
    )

    with pytest.raises(UpstreamError) as raised:
        await source.nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=1)])

    assert raised.value.code == "WEATHER_BAD_RESPONSE"


async def test_the_http_weather_source_reads_rows_from_the_service_envelope():
    rows = [row.model_dump(mode="json") for row in nwp_rows(hours=2)]
    envelope = {"as_of_utc": "2026-01-31T02:00:00Z", "sources": ["ifs025"], "missing": [], "rows": rows}
    source = HttpWeatherSource(base_url="http://weather", transport=httpx.MockTransport(lambda request: httpx.Response(200, json=envelope)))

    got = await source.nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=lead) for lead in (1, 2)])

    assert [row.valid_time_utc for row in got] == [ISSUE_TIME + timedelta(hours=lead) for lead in (1, 2)]


async def test_the_http_weather_source_reports_a_hole_in_the_horizon():
    rows = [row.model_dump(mode="json") for row in nwp_rows(hours=2)]
    source = HttpWeatherSource(base_url="http://weather", transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"rows": rows})))

    with pytest.raises(UpstreamError) as raised:
        await source.nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=lead) for lead in (1, 2, 3)])

    assert raised.value.code == "WEATHER_NO_RUN"


async def test_the_local_weather_source_reads_the_committed_cache():
    rows = await LocalWeatherSource().nwp(
        source="ifs025",
        as_of=ISSUE_TIME,
        valid_times=[ISSUE_TIME + timedelta(hours=lead) for lead in range(1, HORIZON_HOURS + 1)],
    )

    assert len(rows) == HORIZON_HOURS
    assert all(row.available_at_utc <= ISSUE_TIME for row in rows)
    assert all(row.source == "ifs025" for row in rows)


async def test_the_local_weather_source_has_no_run_for_an_unknown_cache(tmp_path):
    with pytest.raises(UpstreamError) as raised:
        await LocalWeatherSource(cache_root=tmp_path).nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=1)])

    assert raised.value.code == "WEATHER_NO_RUN"


async def test_the_local_weather_source_refuses_leaked_rows(monkeypatch):
    from src.forecast.weather.asof import LeakageError

    class Leaking:
        def get_nwp(self, *args, **kwargs):
            raise LeakageError("строка опубликована позже момента прогноза")

    source = LocalWeatherSource()
    source._cached_store = Leaking()

    with pytest.raises(WeatherLeakageError):
        await source.nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=1)])


async def test_the_gateway_switches_to_the_package_when_the_service_is_silent():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сервис погоды не поднят")

    spare = FakeWeather()
    gateway = WeatherGateway(primary=HttpWeatherSource(base_url="http://weather", transport=httpx.MockTransport(handle)), spare=spare)

    rows = await gateway.nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=1)])

    assert len(rows) == HORIZON_HOURS
    assert gateway.used_spare is True
    assert [code for code, _ in gateway.drain_notes()] == ["WEATHER_UNAVAILABLE"]


async def test_the_gateway_trusts_the_service_when_it_says_there_is_no_run():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NO_RUN_AVAILABLE", "message": "прогона нет", "details": {}}})

    gateway = WeatherGateway(primary=HttpWeatherSource(base_url="http://weather", transport=httpx.MockTransport(handle)), spare=FakeWeather())

    with pytest.raises(UpstreamError) as raised:
        await gateway.nwp(source="ifs025", as_of=ISSUE_TIME, valid_times=[ISSUE_TIME + timedelta(hours=1)])

    assert raised.value.code == "WEATHER_NO_RUN"
    assert gateway.used_spare is False


async def test_switching_to_the_spare_weather_is_written_down(client: AsyncClient, user: User, monkeypatch):
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("сервис погоды не поднят")

    # Шлюз, как и в приложении, создается заново на каждый запрос.
    def gateway() -> WeatherGateway:
        return WeatherGateway(primary=HttpWeatherSource(base_url="http://weather", transport=httpx.MockTransport(handle)), spare=FakeWeather())

    monkeypatch.setattr(orchestrator, "weather_source", gateway)
    monkeypatch.setattr(orchestrator, "ml_client", lambda: MlClient(base_url="http://ml", transport=ml_transport()))

    log = (await client.get(f"/api/forecast/{ISSUE_PATH}/agent-log", headers=auth(user))).json()
    body = (await client.get(f"/api/forecast/{ISSUE_PATH}", headers=auth(user))).json()

    assert body["data_source"] == "live"
    switch = next(row for row in log if row["decision"] == "use_spare_weather")
    assert switch["reason_code"] == "WEATHER_UNAVAILABLE"
    assert switch["level"] == "WARN"


async def test_the_weather_page_shows_the_runs_the_agent_sees(client: AsyncClient, user: User, monkeypatch):
    monkeypatch.setattr(orchestrator, "weather_source", LocalWeatherSource)
    monkeypatch.setattr(orchestrator, "ml_client", lambda: MlClient(base_url="http://ml", transport=ml_transport()))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}/weather", headers=auth(user))).json()

    assert body["data_source"] == "live"
    assert body["models"] and all(len(model["wind_ms"]) == HORIZON_HOURS for model in body["models"])
    assert len(body["ensemble_wind_ms"]) == HORIZON_HOURS
    used = [run for run in body["runs"] if run["status"] == "used"]
    assert used and all(run["available_at_utc"] <= body["issue_time_utc"] for run in used)


async def test_the_weather_page_falls_back_to_the_stub_without_any_run(client: AsyncClient, user: User, tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "weather_source", lambda: LocalWeatherSource(cache_root=tmp_path))
    monkeypatch.setattr(orchestrator, "ml_client", lambda: MlClient(base_url="http://ml", transport=ml_transport()))

    body = (await client.get(f"/api/forecast/{ISSUE_PATH}/weather", headers=auth(user))).json()

    assert body["data_source"] == "stub"
