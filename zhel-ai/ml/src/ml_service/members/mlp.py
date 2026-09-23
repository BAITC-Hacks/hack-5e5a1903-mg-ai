"""Участник ансамбля на нейросети: MLPRegressor из scikit-learn.

Вход общий для всех участников: колонки FEATURES в этом порядке, одна строка на час
и турбину. Час приходит числом 0…23, а в сеть уходит как sin/cos, чтобы 23 и 0 были
рядом. Масштабирование делает StandardScaler внутри Pipeline, поэтому сохраненный
файл содержит всё, что нужно для прогноза. Выход обрезается в долю номинала 0…1.
"""

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.neural_network import MLPRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

FEATURES = ["ws", "t", "hour", "doy_sin", "doy_cos", "turbine"]
MODEL_FILE = "mlp.joblib"
RANDOM_STATE = 42


def to_matrix(features: pd.DataFrame) -> np.ndarray:
    """Признаки FEATURES -> матрица сети: ws, t, hour_sin, hour_cos, doy_sin, doy_cos, turbine."""
    missing = [name for name in FEATURES if name not in features.columns]
    if missing:
        raise ValueError(f"Нет признаков {missing}, нужны {FEATURES}")
    hour = 2 * np.pi * features["hour"].to_numpy(dtype=float) / 24
    return np.column_stack(
        [
            features["ws"].to_numpy(dtype=float),
            features["t"].to_numpy(dtype=float),
            np.sin(hour),
            np.cos(hour),
            features["doy_sin"].to_numpy(dtype=float),
            features["doy_cos"].to_numpy(dtype=float),
            features["turbine"].to_numpy(dtype=float),
        ]
    )


def build_pipeline() -> Pipeline:
    network = MLPRegressor(
        hidden_layer_sizes=(64, 32),
        alpha=1e-4,
        learning_rate_init=1e-3,
        batch_size=256,
        max_iter=300,
        early_stopping=True,
        validation_fraction=0.1,
        n_iter_no_change=15,
        random_state=RANDOM_STATE,
    )
    return Pipeline([("scale", StandardScaler()), ("mlp", network)])


class MlpMember:
    name = "mlp"

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline

    def predict_p50(self, features: pd.DataFrame) -> np.ndarray:
        return np.clip(self.pipeline.predict(to_matrix(features)), 0.0, 1.0)


def load(artifacts_dir: Path | str) -> MlpMember:
    """Читает ``<artifacts_dir>/mlp/mlp.joblib``, сохраненный ``training/train_mlp.py``."""
    return MlpMember(joblib.load(Path(artifacts_dir) / "mlp" / MODEL_FILE))
