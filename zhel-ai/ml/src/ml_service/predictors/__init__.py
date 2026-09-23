"""Модели прогноза за одним интерфейсом.

Сервис загружает модель один раз при старте и дальше только читает ее, поэтому состояния
между запросами нет, и реплики сервиса взаимозаменяемы.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pandas as pd

from ml_service.frame import Frame
from ml_service.predictors.baseline import BaselinePredictor
from ml_service.schemas import ModelInfo, ModelMetrics, Turbine

logger = logging.getLogger(__name__)

MODEL_INFO_FILE = "model_info.json"
METRICS_FILE = "metrics.json"


class Predictor(Protocol):
    info: ModelInfo

    def predict(self, frame: Frame, turbines: list[Turbine]) -> pd.DataFrame:
        """Строка на час и турбину: valid_time_utc, turbine, p10, p50, p90 — доли от номинала.

        Сортировку квантилей, обрезку в [0, 1] и расширение интервала делает сервис.
        """
        ...


@dataclass(frozen=True)
class LoadedModel:
    predictor: Predictor
    metrics: ModelMetrics | None
    trained: bool


def load_model(artifacts_dir: Path) -> LoadedModel:
    metrics = _read_metrics(artifacts_dir / METRICS_FILE)
    info_path = artifacts_dir / MODEL_INFO_FILE
    if info_path.exists():
        try:
            info = ModelInfo.model_validate_json(info_path.read_text(encoding="utf-8"))
            if info.kind == "lightgbm_quantile":
                # Модуль lgbm.py добавляет обучающая сессия вместе с файлами модели, см. ml/README.md.
                from ml_service.predictors import lgbm

                return LoadedModel(lgbm.load(artifacts_dir, info), metrics, trained=True)
        except Exception:
            logger.exception("Не удалось загрузить обученную модель из %s, работаю на кривой мощности", artifacts_dir)
    return LoadedModel(BaselinePredictor(), metrics, trained=False)


def _read_metrics(path: Path) -> ModelMetrics | None:
    if not path.exists():
        return None
    try:
        return ModelMetrics.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Файл %s не соответствует схеме ModelMetrics", path)
        return None
