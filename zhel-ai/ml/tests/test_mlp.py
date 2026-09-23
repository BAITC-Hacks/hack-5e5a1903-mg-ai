import numpy as np
import pandas as pd
import pytest

from ml_service.config import DEFAULT_ARTIFACTS_DIR
from ml_service.members import mlp


def _features(rows: int = 96, ws=None) -> pd.DataFrame:
    angle = 2 * np.pi * 45 / 365.25
    return pd.DataFrame(
        {
            "ws": np.linspace(0, 20, rows) if ws is None else ws,
            "t": np.full(rows, -5.0),
            "hour": np.arange(rows) % 24,
            "doy_sin": np.sin(angle),
            "doy_cos": np.cos(angle),
            "turbine": np.arange(rows) // (rows // 2),
        }
    )[mlp.FEATURES]


@pytest.fixture(scope="module")
def member() -> mlp.MlpMember:
    return mlp.load(DEFAULT_ARTIFACTS_DIR)


def test_predict_p50_shape_and_range(member):
    p50 = member.predict_p50(_features())
    assert p50.shape == (96,)
    assert np.all((p50 >= 0) & (p50 <= 1))


def test_predict_p50_follows_power_curve(member):
    calm, mid, rated = member.predict_p50(_features(3, ws=[2.0, 7.0, 13.0]).assign(turbine=0))
    assert calm < 0.1
    assert calm < mid < rated
    assert rated > 0.85


def test_predict_p50_is_repeatable(member):
    features = _features()
    assert np.array_equal(member.predict_p50(features), member.predict_p50(features))


def test_hour_is_encoded_cyclically():
    matrix = mlp.to_matrix(_features(24).assign(hour=[0, 23] * 12))
    midnight, late = matrix[0], matrix[1]
    assert np.hypot(midnight[2] - late[2], midnight[3] - late[3]) < 0.3


def test_missing_feature_is_rejected(member):
    with pytest.raises(ValueError, match="doy_cos"):
        member.predict_p50(_features().drop(columns="doy_cos"))


@pytest.mark.filterwarnings("ignore::sklearn.exceptions.ConvergenceWarning")
def test_training_is_deterministic():
    rng = np.random.default_rng(0)
    features = _features(400, ws=rng.uniform(0, 20, 400))
    target = np.clip((features["ws"].to_numpy() - 3) / 8, 0, 1)
    runs = [mlp.build_pipeline().set_params(mlp__max_iter=20).fit(mlp.to_matrix(features), target) for _ in range(2)]
    first, second = (run.predict(mlp.to_matrix(features)) for run in runs)
    assert np.array_equal(first, second)
