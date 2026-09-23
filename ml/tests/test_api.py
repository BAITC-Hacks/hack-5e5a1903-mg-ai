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


def test_predict_returns_every_hour_for_both_turbines(client):
    response = client.post("/predict", json=make_request())
    assert response.status_code == 200
    body = response.json()
    assert len(body["forecast"]) == 48 * 2
    assert [p["turbine"] for p in body["forecast"][:2]] == ["T1", "T2"]
    assert {p["lead_h"] for p in body["forecast"]} == set(range(1, 49))
    assert all(0 <= p["p10"] <= p["p50"] <= p["p90"] <= 1 for p in body["forecast"])
    assert body["capacity_mw"] == {"T1": 2.5, "T2": 2.5}
    assert len(body["hourly_inputs"]) == 48
    assert body["hourly_inputs"][0]["sources"] == ["ecmwf_ifs", "gfs_global"]
    assert body["hourly_inputs"][0]["wind_spread_ms"] is not None
    assert [w["code"] for w in body["warnings"]] == ["BASELINE_MODEL"]
    assert body["degraded"] is True
    assert body["request_id"] == "test"


def test_station_is_available_on_request(client):
    body = client.post("/predict", json=make_request(options={"turbines": ["station"]})).json()
    assert {p["turbine"] for p in body["forecast"]} == {"station"}
    assert body["capacity_mw"] == {"station": 5.0}


def test_rows_from_weather_service_are_accepted_as_is(client):
    body = make_request()
    for row in body["rows"]:
        row["field_added_later"] = "ignored"
    assert client.post("/predict", json=body).status_code == 200


def test_wind_shift_scenario_raises_wind_and_power(client):
    base = client.post("/predict", json=make_request()).json()
    shifted = client.post("/predict", json=make_request(options={"wind_shift_ms": 2.0})).json()
    assert shifted["hourly_inputs"][0]["wind_speed_hub_ms"] > base["hourly_inputs"][0]["wind_speed_hub_ms"]
    assert sum(p["p50"] for p in shifted["forecast"]) > sum(p["p50"] for p in base["forecast"])


def test_interval_scale_widens_the_band(client):
    base = client.post("/predict", json=make_request()).json()["forecast"]
    wide = client.post("/predict", json=make_request(options={"interval_scale": 2.0})).json()["forecast"]
    widths = [(w["p90"] - w["p10"]) - (b["p90"] - b["p10"]) for b, w in zip(base, wide, strict=True)]
    assert all(d >= -1e-9 for d in widths)
    assert any(d > 0 for d in widths)


def test_weather_published_after_issue_time_is_rejected(client):
    body = make_request()
    body["rows"][5]["available_at_utc"] = "2026-01-31T02:30:00Z"
    response = client.post("/predict", json=body)
    assert response.status_code == 422
    assert error_code(response) == "LEAKAGE_DETECTED"


def test_hour_without_wind_is_rejected(client):
    body = make_request()
    body["rows"] = [row for row in body["rows"] if row["valid_time_utc"] != "2026-01-31T12:00:00+00:00"]
    response = client.post("/predict", json=body)
    assert response.status_code == 422
    assert error_code(response) == "INSUFFICIENT_INPUTS"
    assert response.json()["error"]["details"]["examples"] == ["2026-01-31T12:00:00+00:00"]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (lambda b: b["rows"][0].update(ws100=-1.0), "VALIDATION_ERROR"),
        (lambda b: b.update(weather=b["rows"]), "VALIDATION_ERROR"),
        (lambda b: b.update(issue_time_utc="2026-01-31T02:30:00Z"), "ISSUE_TIME_NOT_ON_HOUR"),
        (lambda b: b.update(horizon_hours=24), "OUT_OF_HORIZON"),
        (lambda b: b["rows"].append(dict(b["rows"][0])), "DUPLICATE_ROWS"),
        (lambda b: b["rows"][0].update(run_init_utc="2026-01-31T01:45:00Z"), "INCONSISTENT_RUN_TIMES"),
    ],
)
def test_contract_violations_return_error_envelope(client, change, code):
    body = make_request()
    change(body)
    response = client.post("/predict", json=body)
    assert response.status_code == 422
    assert error_code(response) == code


def test_required_weather_model_must_cover_the_horizon():
    predictor = BaselinePredictor()
    inputs = ModelInputs(
        sources=[SourceSpec(name="ecmwf_ifs", required=True), SourceSpec(name="icon_global", required=False)],
        variables=["ws100"],
        hub_height_m=80,
        max_horizon_hours=48,
    )
    predictor.info = predictor.info.model_copy(update={"inputs": inputs})
    model = LoadedModel(predictor, metrics=None, trained=True)

    with pytest.raises(MLServiceError) as error:
        predict(PredictRequest.model_validate(make_request(sources=("gfs_global",))), model)
    assert error.value.code == "MISSING_REQUIRED_SOURCE"

    response = predict(PredictRequest.model_validate(make_request(sources=("ecmwf_ifs",))), model)
    assert [w.code for w in response.warnings] == ["SOURCE_MISSING"]
    assert response.degraded is True


def test_model_info_describes_the_baseline(client):
    info = client.get("/model-info").json()
    assert info["kind"] == "baseline_power_curve"
    assert info["quantiles"] == [0.1, 0.5, 0.9]
    assert info["features"] == [{"name": "wind_speed_hub", "importance": 1.0, "description": "Ветер на 80 м, среднее по моделям погоды"}]
    assert info["power_curve"][0] == {"wind_ms": 0.0, "power_norm": 0.0}
    assert max(p["power_norm"] for p in info["power_curve"]) == 1.0


def test_metrics_are_404_until_the_report_exists(client):
    response = client.get("/metrics")
    assert response.status_code == 404
    assert error_code(response) == "METRICS_NOT_AVAILABLE"


def test_metrics_are_served_from_artifacts(artifacts_dir):
    report = {
        "model_version": "lgbm-test",
        "period_start_utc": "2026-01-01T00:00:00Z",
        "period_end_utc": "2026-02-01T00:00:00Z",
        "nmae_d1_pct": 16.4,
        "nmae_d2_pct": 17.6,
        "nrmse_48_pct": 23.0,
        "skill_vs_persistence_pct": 41.0,
        "coverage_p10_p90_pct": 78.0,
        "baselines": [{"name": "climatology", "nmae_pct": 31.0}],
        "by_day": [],
        "by_lead": [{"lead_h": 1, "nmae_pct": 15.0}],
    }
    (artifacts_dir / "metrics.json").write_text(json.dumps(report), encoding="utf-8")
    from ml_service.main import app

    with TestClient(app) as client:
        body = client.get("/metrics").json()
    assert body["model_version"] == "lgbm-test"
    assert body["nmae_d1_pct"] == 16.4
    assert body["series"] == []
