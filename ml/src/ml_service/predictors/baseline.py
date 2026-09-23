"""Физическая заглушка: паспортная кривая Goldwind GW109/2500 на ветре ступицы.

Работает, пока обученной модели нет, и нужна, чтобы backend мог подключиться к сервису
до конца обучения. Ответ помечается предупреждением BASELINE_MODEL.
"""

import numpy as np
import pandas as pd

from ml_service.frame import HUB_HEIGHT_M, Frame
from ml_service.schemas import (
    ALL_TARGETS,
    CAPACITY_MW,
    WIND_SPEED_VARIABLES,
    FeatureImportance,
    FeatureSpec,
    ModelCard,
    ModelInputs,
    PowerCurvePoint,
    Target,
)

CUT_IN_MS = 3.0
RATED_MS = 10.3
CUT_OUT_MS = 25.0
# Квантиль стандартного нормального распределения для P10 и P90.
Z_P90 = 1.2816


def passport_curve(wind) -> np.ndarray:
    v = np.asarray(wind, dtype=float)
    power = np.clip((v**3 - CUT_IN_MS**3) / (RATED_MS**3 - CUT_IN_MS**3), 0.0, 1.0)
    return np.where((v < CUT_IN_MS) | (v >= CUT_OUT_MS), 0.0, power)


class BaselinePredictor:
    def __init__(self) -> None:
        self.card = ModelCard(
            name="baseline-power-curve",
            version="baseline-gw109-v1",
            kind="baseline_power_curve",
            quantiles=[0.1, 0.5, 0.9],
            targets=list(ALL_TARGETS),
            capacity_mw=dict(CAPACITY_MW),
            inputs=ModelInputs(sources=[], variables=list(WIND_SPEED_VARIABLES), hub_height_m=HUB_HEIGHT_M, max_horizon_hours=48),
            features=[FeatureSpec(name="wind_speed_hub", description="Ветер на 80 м, среднее по моделям погоды")],
            feature_importance=[FeatureImportance(feature="wind_speed_hub", importance=1.0)],
            power_curve=[PowerCurvePoint(wind_speed_ms=float(v), power=round(float(passport_curve(v)), 4)) for v in np.arange(0.0, 26.5, 0.5)],
            notes="Заглушка до появления обученной модели: паспортная кривая GW109 (3 / 10,3 / 25 м/с), "
            "интервал P10–P90 растет с заблаговременностью и разбросом моделей погоды",
        )

    def predict(self, frame: Frame, targets: list[Target]) -> pd.DataFrame:
        summary = frame.summary
        wind = summary["wind_speed_hub_ms"].to_numpy(dtype=float)
        sigma = 0.8 + 0.03 * summary["lead_h"].to_numpy(dtype=float) + summary["wind_spread_ms"].fillna(0.0).to_numpy(dtype=float)
        base = pd.DataFrame(
            {
                "valid_time_utc": summary.index,
                "p10": passport_curve(np.clip(wind - Z_P90 * sigma, 0.0, None)),
                "p50": passport_curve(wind),
                "p90": passport_curve(wind + Z_P90 * sigma),
            }
        )
        return pd.concat([base.assign(target=target) for target in targets], ignore_index=True)
