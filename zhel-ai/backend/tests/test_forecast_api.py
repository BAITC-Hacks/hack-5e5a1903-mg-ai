"""Тесты контракта прогноза: формы ответов, на которые опирается интерфейс.

Проверяется именно контракт, а не конкретные числа: наполнение пока синтетическое
и будет заменено данными сервисов погоды и модели, а формы полей останутся.
"""

from httpx import AsyncClient

from src.core.security import create_access_token
from src.modules.auth.models import User
from src.modules.forecast.service import FIRST_ISSUE, HORIZON_HOURS, LAST_ISSUE, TURBINES

ISSUE = FIRST_ISSUE.isoformat()


def auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


async def test_forecast_requires_a_token(client: AsyncClient):
    response = await client.get(f"/api/forecast/{ISSUE}")

    assert response.status_code == 401


async def test_issue_list_covers_the_whole_replay(client: AsyncClient, user: User):
    response = await client.get("/api/forecast/issues", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 28
    first, last = body["items"][0], body["items"][-1]
    assert first["issue_date"] == FIRST_ISSUE.isoformat()
    assert last["issue_date"] == LAST_ISSUE.isoformat()
    assert first["target_date"] > first["issue_date"]


async def test_forecast_returns_48_hours_for_each_turbine(client: AsyncClient, user: User):
    response = await client.get(f"/api/forecast/{ISSUE}", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    hours = body["hours"]
    assert len(hours) == HORIZON_HOURS * len(TURBINES)
    assert {hour["turbine"] for hour in hours} == {turbine.name for turbine in TURBINES}
    assert sorted({hour["lead_h"] for hour in hours}) == list(range(1, HORIZON_HOURS + 1))
    assert all(hour["p10"] <= hour["p50"] <= hour["p90"] for hour in hours)
    assert all(0.0 <= hour["p50"] <= 1.0 for hour in hours)


async def test_forecast_says_the_numbers_are_synthetic(client: AsyncClient, user: User):
    response = await client.get(f"/api/forecast/{ISSUE}", headers=auth(user))

    assert response.json()["data_source"] == "stub"


async def test_same_day_is_answered_the_same_way(client: AsyncClient, user: User):
    first = await client.get(f"/api/forecast/{ISSUE}", headers=auth(user))
    second = await client.get(f"/api/forecast/{ISSUE}", headers=auth(user))

    assert first.json() == second.json()


async def test_unknown_day_uses_the_error_envelope(client: AsyncClient, user: User):
    response = await client.get("/api/forecast/2025-06-01", headers=auth(user))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ISSUE_NOT_FOUND"


async def test_agent_log_walks_the_steps_of_the_case(client: AsyncClient, user: User):
    response = await client.get(f"/api/forecast/{ISSUE}/agent-log", headers=auth(user))

    assert response.status_code == 200
    steps = [row["step"] for row in response.json()]
    assert steps[:1] == ["fetch_weather"]
    assert "run_model" in steps
    assert steps[-1] == "recompute_on_update"


async def test_weather_reports_run_availability(client: AsyncClient, user: User):
    response = await client.get(f"/api/forecast/{ISSUE}/weather", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert {run["status"] for run in body["runs"]} <= {"used", "stale", "after_issue"}
    assert len(body["ensemble_wind_ms"]) == HORIZON_HOURS
    assert all(len(model["wind_ms"]) == HORIZON_HOURS for model in body["models"])


async def test_dispatch_bid_grows_with_accepted_risk(client: AsyncClient, user: User):
    careful = await client.get(f"/api/forecast/{ISSUE}/dispatch", params={"risk": 0.1}, headers=auth(user))
    bold = await client.get(f"/api/forecast/{ISSUE}/dispatch", params={"risk": 0.5}, headers=auth(user))

    assert careful.status_code == bold.status_code == 200
    assert len(careful.json()["hours"]) == 24
    assert bold.json()["kpi"]["day_bid_mwh"] > careful.json()["kpi"]["day_bid_mwh"]


async def test_dispatch_rejects_risk_outside_the_slider(client: AsyncClient, user: User):
    response = await client.get(f"/api/forecast/{ISSUE}/dispatch", params={"risk": 0.9}, headers=auth(user))

    assert response.status_code == 422


async def test_backtest_compares_against_baselines(client: AsyncClient, user: User):
    response = await client.get("/api/forecast/backtest", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert len(body["by_lead"]) == HORIZON_HOURS
    assert len(body["by_day"]) == 28
    assert {baseline["name"] for baseline in body["baselines"]}


async def test_site_describes_both_turbines(client: AsyncClient, user: User):
    response = await client.get("/api/forecast/site", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert len(body["turbines"]) == 2
    assert body["capacity_mw"] == 5.0


async def test_model_page_has_a_power_curve(client: AsyncClient, user: User):
    response = await client.get("/api/forecast/model", headers=auth(user))

    assert response.status_code == 200
    body = response.json()
    assert body["quantiles"] == [0.1, 0.5, 0.9]
    assert body["power_curve"][0]["power_norm"] == 0.0
    assert body["power_curve"][-1]["power_norm"] == 0.0
