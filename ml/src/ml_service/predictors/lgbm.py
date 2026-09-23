"""LightGBM на SCADA: P50 — среднее бустеров с разными сидами, P10/P90 — P50 плюс поправки калибровки.

Поправки посчитаны по архивным прогнозам погоды и зависят от уровня прогноза, разброса
моделей погоды и заблаговременности, поэтому интервал отражает ошибку прогноза погоды,
а не только разброс кривой мощности.
"""

import importlib
import json
import logging
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from ml_service.features import FEATURES, features_from_frame
from ml_service.frame import Frame
from ml_service.schemas import ModelInfo, Turbine

CALIBRATION_FILE = "calibration.json"
# Другие модели ансамбля: ml_service.members.<имя>.load(artifacts_dir) -> объект с predict_p50(features).
# На проверке Б все три модели дают почти одинаковую ошибку, поэтому P50 — простое среднее.
# Если модуль или файлы модели не найдены, она просто не участвует.
MEMBERS = ("extratrees", "mlp")

logger = logging.getLogger(__name__)


class Calibration:
    def __init__(self, data: dict):
        self.level_edges = np.asarray(data["level_edges"], dtype=float)
        self.spread_edges = np.asarray(data["spread_edges"], dtype=float)
        self.lead_edge = int(data["lead_edge"])
        self.q10 = np.asarray(data["q10"], dtype=float)
        self.q90 = np.asarray(data["q90"], dtype=float)

    def bins(self, p50: np.ndarray, spread: np.ndarray, lead: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        level = np.digitize(p50, self.level_edges)
        # Один источник погоды: разброса нет, берем самую широкую корзину.
        spread_bin = np.where(np.isnan(spread), len(self.spread_edges), np.digitize(np.nan_to_num(spread), self.spread_edges))
        return (lead > self.lead_edge).astype(int), level, spread_bin

    def interval(self, p50: np.ndarray, spread: np.ndarray, lead: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lead_bin, level, spread_bin = self.bins(p50, spread, lead)
        return p50 + self.q10[lead_bin, level, spread_bin], p50 + self.q90[lead_bin, level, spread_bin]


class LgbmPredictor:
    def __init__(self, info: ModelInfo, boosters: list[lgb.Booster], calibration: Calibration, members: dict | None = None):
        self.info = info
        self.boosters = boosters
        self.calibration = calibration
        self.members = members or {}

    def p50(self, features: pd.DataFrame) -> np.ndarray:
        values = features[FEATURES].to_numpy(dtype=float)
        predictions = [np.mean([booster.predict(values) for booster in self.boosters], axis=0)]
        predictions += [np.asarray(member.predict_p50(features[FEATURES]), dtype=float) for member in self.members.values()]
        return np.clip(np.mean(np.clip(predictions, 0.0, 1.0), axis=0), 0.0, 1.0)

    def predict(self, frame: Frame, turbines: list[Turbine]) -> pd.DataFrame:
        summary = frame.summary
        spread = summary["wind_spread_ms"].to_numpy(dtype=float)
        lead = summary["lead_h"].to_numpy(dtype=int)
        per_turbine = {}
        for turbine in ("T1", "T2"):
            p50 = self.p50(features_from_frame(frame, turbine))
            p10, p90 = self.calibration.interval(p50, spread, lead)
            per_turbine[turbine] = (p10, p50, p90)
        per_turbine["station"] = tuple((a + b) / 2 for a, b in zip(per_turbine["T1"], per_turbine["T2"], strict=True))
        parts = [
            pd.DataFrame({"valid_time_utc": summary.index, "turbine": turbine, "p10": q[0], "p50": q[1], "p90": q[2]})
            for turbine, q in ((t, per_turbine[t]) for t in turbines)
        ]
        return pd.concat(parts, ignore_index=True)


def load(artifacts_dir: Path, info: ModelInfo) -> LgbmPredictor:
    files = sorted(artifacts_dir.glob("lgbm_p50_s*.txt"))
    if not files:
        raise FileNotFoundError(f"Нет бустеров lgbm_p50_s*.txt в {artifacts_dir}")
    boosters = [lgb.Booster(model_file=str(path)) for path in files]
    calibration = Calibration(json.loads((artifacts_dir / CALIBRATION_FILE).read_text(encoding="utf-8")))
    return LgbmPredictor(info, boosters, calibration, _load_members(artifacts_dir))


def _load_members(artifacts_dir: Path) -> dict:
    members = {}
    for name in MEMBERS:
        try:
            members[name] = importlib.import_module(f"ml_service.members.{name}").load(artifacts_dir)
        except Exception as exc:  # noqa: BLE001 — отсутствие члена ансамбля не должно ронять сервис
            logger.warning("Модель ансамбля %s не подключена: %s", name, exc)
    logger.info("Ансамбль: lightgbm + %s", ", ".join(members) or "без других моделей")
    return members
