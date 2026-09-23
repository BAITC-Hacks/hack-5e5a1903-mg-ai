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
from ml_service.schemas import BacktestReport, ModelCard, Target

logger = logging.getLogger(__name__)

MODEL_CARD_FILE = "model_card.json"
BACKTEST_FILE = "backtest.json"


class Predictor(Protocol):
    card: ModelCard

    def predict(self, frame: Frame, targets: list[Target]) -> pd.DataFrame:
        """Строка на час и цель: valid_time_utc, target, p10, p50, p90 — доли от номинала.

        Сортировку квантилей, обрезку в [0, 1] и расширение интервала делает сервис.
        """
        ...


@dataclass(frozen=True)
class LoadedModel:
    predictor: Predictor
    backtest: BacktestReport | None
    trained: bool


def load_model(artifacts_dir: Path) -> LoadedModel:
    backtest = _read_backtest(artifacts_dir / BACKTEST_FILE)
    card_path = artifacts_dir / MODEL_CARD_FILE
    if card_path.exists():
        try:
            card = ModelCard.model_validate_json(card_path.read_text(encoding="utf-8"))
            if card.kind == "lightgbm_quantile":
                # Модуль lgbm.py добавляет обучающая сессия вместе с файлами модели, см. ml/README.md.
                from ml_service.predictors import lgbm

                return LoadedModel(lgbm.load(artifacts_dir, card), backtest, trained=True)
        except Exception:
            logger.exception("Не удалось загрузить обученную модель из %s, работаю на кривой мощности", artifacts_dir)
    return LoadedModel(BaselinePredictor(), backtest, trained=False)


def _read_backtest(path: Path) -> BacktestReport | None:
    if not path.exists():
        return None
    try:
        return BacktestReport.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Файл %s не соответствует схеме BacktestReport", path)
        return None
