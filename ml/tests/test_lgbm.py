from pathlib import Path

import numpy as np
import pandas as pd

from ml_service.frame import build_frame_from_rows
from ml_service.predictors import load_model

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts"
ISSUE = pd.Timestamp("2026-01-31T02:00:00Z")


def _rows(sources: list[str]) -> pd.DataFrame:
    valid = pd.date_range(ISSUE + pd.Timedelta(hours=1), periods=48, freq="h")
    wind = 3 + 9 * (1 + np.sin(np.arange(48) / 6)) / 2
    parts = [
        pd.DataFrame(
            {
                "valid_time_utc": valid,
                "source": source,
                "run_init_utc": pd.Timestamp("2026-01-30T12:00:00Z"),
                "available_at_utc": pd.Timestamp("2026-01-30T22:00:00Z"),
                "ws80": wind + n * 0.5,
                "t2m": -5.0,
            }
        )
        for n, source in enumerate(sources)
    ]
    return pd.concat(parts, ignore_index=True)


def test_trained_model_loads_and_predicts_every_turbine():
    model = load_model(ARTIFACTS)
    assert model.trained and model.predictor.info.kind == "lightgbm_quantile"
    frame = build_frame_from_rows(ISSUE, 48, _rows(["ifs025", "gfs", "icon", "gem"]))
    out = model.predictor.predict(frame, ["T1", "T2", "station"])
    assert len(out) == 48 * 3
    assert set(out["turbine"]) == {"T1", "T2", "station"}
    assert out[["p10", "p50", "p90"]].notna().all().all()
    assert (out["p50"].between(0, 1)).all()


def test_prediction_is_deterministic_and_works_with_one_source():
    model = load_model(ARTIFACTS)
    frame = build_frame_from_rows(ISSUE, 48, _rows(["gfs"]))
    first = model.predictor.predict(frame, ["T1"])
    second = model.predictor.predict(frame, ["T1"])
    pd.testing.assert_frame_equal(first, second)
    assert (first["p90"] - first["p10"]).gt(0).any()


def test_stronger_wind_gives_more_power():
    model = load_model(ARTIFACTS)
    calm = model.predictor.predict(build_frame_from_rows(ISSUE, 48, _rows(["gfs"]).assign(ws80=4.0)), ["T1"])
    windy = model.predictor.predict(build_frame_from_rows(ISSUE, 48, _rows(["gfs"]).assign(ws80=11.0)), ["T1"])
    assert windy["p50"].mean() > calm["p50"].mean() + 0.3
