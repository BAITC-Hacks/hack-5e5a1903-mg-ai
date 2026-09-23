import json

import pytest
from fastapi.testclient import TestClient

from ml_service.errors import MLServiceError
from ml_service.predictors import LoadedModel
from ml_service.predictors.baseline import BaselinePredictor
from ml_service.schemas import ModelInputs, PredictRequest, SourceSpec
from ml_service.service import predict
from tests.conftest import make_request


def error_code(response) -> str:
    return response.json()["error"]["code"]


def test_health_reports_baseline_until_model_is_trained(client):
    body = client.get("/health").json()
    assert body == {"status": "degraded", "model_loaded": False, "model_version": "baseline-gw109-v1", "model_kind": "baseline_power_curve"}


def test_predict_returns_every_hour_for_every_target(client):
    response = client.post("/v1/predict", json=make_request())
    assert response.status_code == 200
    body = response.json()
    assert len(body["forecast"]) == 48 * 3
    assert [p["target"] for p in body["forecast"][:3]] == ["T1", "T2", "station"]
    assert {p["lead_h"] for p in body["forecast"]} == set(range(1, 49))
    assert all(0 <= p["p10"] <= p["p50"] <= p["p90"] <= 1 for p in body["forecast"])
    assert body["capacity_mw"] == {"T1": 2.5, "T2": 2.5, "station": 5.0}
    assert len(body["hourly_inputs"]) == 48
    assert body["hourly_inputs"][0]["sources"] == ["ecmwf_ifs025", "gfs_global"]
    assert body["hourly_inputs"][0]["wind_spread_ms"] is not None
    assert [w["code"] for w in body["warnings"]] == ["BASELINE_MODEL"]
    assert body["degraded"] is True
    assert body["request_id"] == "test"


def test_targets_option_limits_the_answer(client):
    body = client.post("/v1/predict", json=make_request(options={"targets": ["station"]})).json()
    assert {p["target"] for p in body["forecast"]} == {"station"}
    assert body["capacity_mw"] == {"station": 5.0}


def test_wind_shift_scenario_raises_wind_and_power(client):
    base = client.post("/v1/predict", json=make_request()).json()
    shifted = client.post("/v1/predict", json=make_request(options={"wind_shift_ms": 2.0})).json()
    assert shifted["hourly_inputs"][0]["wind_speed_hub_ms"] > base["hourly_inputs"][0]["wind_speed_hub_ms"]
    assert sum(p["p50"] for p in shifted["forecast"]) > sum(p["p50"] for p in base["forecast"])


def test_interval_scale_widens_the_band(client):
    base = client.post("/v1/predict", json=make_request()).json()["forecast"]
    wide = client.post("/v1/predict", json=make_request(options={"interval_scale": 2.0})).json()["forecast"]
    widths = [(w["p90"] - w["p10"]) - (b["p90"] - b["p10"]) for b, w in zip(base, wide, strict=True)]
    assert all(d >= -1e-9 for d in widths)
    assert any(d > 0 for d in widths)


def test_weather_published_after_issue_time_is_rejected(client):
    body = make_request()
    body["weather"][5]["available_at_utc"] = "2026-01-31T02:30:00Z"
    response = client.post("/v1/predict", json=body)
    assert response.status_code == 422
    assert error_code(response) == "LEAKAGE_DETECTED"


def test_hour_without_wind_is_rejected(client):
    body = make_request()
    body["weather"] = [row for row in body["weather"] if row["valid_time_utc"] != "2026-01-31T12:00:00+00:00"]
    response = client.post("/v1/predict", json=body)
    assert response.status_code == 422
    assert error_code(response) == "INSUFFICIENT_INPUTS"
    assert response.json()["error"]["details"]["examples"] == ["2026-01-31T12:00:00+00:00"]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda b: b["weather"][0].update(wind_speed_150m=9.0), "VALIDATION_ERROR"),
        (lambda b: b.update(issue_time_utc="2026-01-31T02:30:00Z"), "ISSUE_TIME_NOT_ON_HOUR"),
        (lambda b: b.update(horizon_hours=24), "OUT_OF_HORIZON"),
        (lambda b: b["weather"].append(dict(b["weather"][0])), "DUPLICATE_ROWS"),
        (lambda b: b["weather"][0].update(run_init_utc="2026-01-31T01:45:00Z"), "INCONSISTENT_RUN_TIMES"),
    ],
)
def test_contract_violations_return_error_envelope(client, change, code):
    body = make_request()
    change(body)
    response = client.post("/v1/predict", json=body)
    assert response.status_code == 422
    assert error_code(response) == code


def test_required_weather_model_must_cover_the_horizon():
    predictor = BaselinePredictor()
    inputs = ModelInputs(
        sources=[SourceSpec(name="ecmwf_ifs025", required=True), SourceSpec(name="icon_global", required=False)],
        variables=["wind_speed_100m"],
        hub_height_m=80,
        max_horizon_hours=48,
    )
    predictor.card = predictor.card.model_copy(update={"inputs": inputs})
    model = LoadedModel(predictor, backtest=None, trained=True)

    with pytest.raises(MLServiceError) as error:
        predict(PredictRequest.model_validate(make_request(sources=("gfs_global",))), model)
    assert error.value.code == "MISSING_REQUIRED_SOURCE"

    response = predict(PredictRequest.model_validate(make_request(sources=("ecmwf_ifs025",))), model)
    assert [w.code for w in response.warnings] == ["SOURCE_MISSING"]
    assert response.degraded is True


def test_model_card_describes_the_baseline(client):
    card = client.get("/v1/model").json()
    assert card["kind"] == "baseline_power_curve"
    assert card["quantiles"] == [0.1, 0.5, 0.9]
    assert card["power_curve"][0] == {"wind_speed_ms": 0.0, "power": 0.0}
    assert max(p["power"] for p in card["power_curve"]) == 1.0


def test_backtest_is_404_until_the_report_exists(client):
    response = client.get("/v1/model/backtest")
    assert response.status_code == 404
    assert error_code(response) == "BACKTEST_NOT_AVAILABLE"


def test_backtest_report_is_served_from_artifacts(artifacts_dir):
    report = {
        "model_version": "lgbm-test",
        "summary": {"period_start_utc": "2026-01-01T00:00:00Z", "period_end_utc": "2026-02-01T00:00:00Z", "nmae_pct_lead_1_24": 16.4},
        "issues": [],
        "series": [],
    }
    (artifacts_dir / "backtest.json").write_text(json.dumps(report), encoding="utf-8")
    from ml_service.main import app

    with TestClient(app) as client:
        body = client.get("/v1/model/backtest").json()
    assert body["model_version"] == "lgbm-test"
    assert body["summary"]["nmae_pct_lead_1_24"] == 16.4
