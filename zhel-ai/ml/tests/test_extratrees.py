import numpy as np
import pandas as pd
import pytest
from sklearn.ensemble import ExtraTreesRegressor

from ml_service.config import DEFAULT_ARTIFACTS_DIR
from ml_service.members import extratrees
from training.train_extratrees import check_days, make_features

HOURS = pd.date_range("2026-02-01T03:00Z", periods=48, freq="h")


def issue_features(wind: float) -> pd.DataFrame:
    times = HOURS.repeat(2)
    return make_features(times, np.full(len(times), wind), np.full(len(times), -5.0), ["T1", "T2"] * len(HOURS))


def test_make_features_order_and_encoding():
    features = make_features(pd.DatetimeIndex(["2026-01-01T05:00Z", "2026-07-02T23:00Z"]), [7.5, 12.0], [-3.0, 25.0], ["T1", "T2"])
    assert tuple(features.columns) == extratrees.FEATURES
    assert features["hour"].tolist() == [5.0, 23.0]
    assert features["turbine"].tolist() == [0.0, 1.0]
    assert features["doy_sin"].iloc[0] == pytest.approx(np.sin(2 * np.pi / 365.25))
    assert features[["doy_sin", "doy_cos"]].pow(2).sum(axis=1).to_numpy() == pytest.approx([1.0, 1.0])


def test_check_days_are_fixed_share_of_whole_days():
    times = pd.Series(pd.date_range("2025-01-01", periods=100 * 24, freq="h", tz="UTC"))
    days = check_days(times)
    assert len(days) == 20
    assert days == check_days(times)
    assert all(day == day.normalize() for day in days)


def test_committed_model_predicts_one_issue():
    member = extratrees.load(DEFAULT_ARTIFACTS_DIR)
    calm, windy = member.predict_p50(issue_features(2.0)), member.predict_p50(issue_features(12.0))
    assert calm.shape == windy.shape == (96,)
    assert np.all((calm >= 0) & (calm <= 1.05)) and np.all((windy >= 0) & (windy <= 1.05))
    assert windy.mean() > calm.mean() + 0.5


def test_predict_requires_exact_column_order(tmp_path):
    features = issue_features(8.0)
    model = ExtraTreesRegressor(n_estimators=3, random_state=0).fit(features, features["ws"] / 20)
    (tmp_path / extratrees.MEMBER_DIR).mkdir()
    extratrees.joblib.dump(model, tmp_path / extratrees.MEMBER_DIR / extratrees.MODEL_FILE)
    member = extratrees.load(tmp_path)

    assert member.predict_p50(features).shape == (96,)
    with pytest.raises(ValueError, match="в этом порядке"):
        member.predict_p50(features[list(reversed(extratrees.FEATURES))])


def test_load_rejects_foreign_model(tmp_path):
    (tmp_path / extratrees.MEMBER_DIR).mkdir()
    extratrees.joblib.dump({"not": "a model"}, tmp_path / extratrees.MEMBER_DIR / extratrees.MODEL_FILE)
    with pytest.raises(TypeError):
        extratrees.load(tmp_path)
