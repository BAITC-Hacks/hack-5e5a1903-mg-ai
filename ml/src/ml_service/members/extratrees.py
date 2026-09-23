"""Участник ансамбля на ExtraTreesRegressor из scikit-learn.

Одна модель на обе турбины: по ветру, температуре, часу, сезону и номеру турбины отдает
P50 доли мощности от номинала. Обучение: ``ml/training/train_extratrees.py``, файлы
модели лежат в ``<artifacts_dir>/extratrees/``.
"""

from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor

# Колонки таблицы признаков строго в этом порядке, общие для всех участников ансамбля.
FEATURES = ("ws", "t", "hour", "doy_sin", "doy_cos", "turbine")
MEMBER_DIR = "extratrees"
MODEL_FILE = "model.joblib"
METRICS_FILE = "metrics.json"


@dataclass(frozen=True)
class ExtraTreesMember:
    model: ExtraTreesRegressor

    def predict_p50(self, features: pd.DataFrame) -> np.ndarray:
        """P50 доли мощности на каждую строку ``features``. Обрезку в [0, 1] делает сервис."""
        if tuple(features.columns) != FEATURES:
            raise ValueError(f"Нужны колонки {list(FEATURES)} в этом порядке, пришли {list(features.columns)}")
        return self.model.predict(features)


def load(artifacts_dir: Path | str) -> ExtraTreesMember:
    """Читает модель из ``<artifacts_dir>/extratrees/model.joblib``."""
    model = joblib.load(Path(artifacts_dir) / MEMBER_DIR / MODEL_FILE)
    if not isinstance(model, ExtraTreesRegressor):
        raise TypeError(f"В {MEMBER_DIR}/{MODEL_FILE} не ExtraTreesRegressor, а {type(model).__name__}")
    return ExtraTreesMember(model)
