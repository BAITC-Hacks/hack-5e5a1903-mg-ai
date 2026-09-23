"""Признаки модели: одна функция для обучения на SCADA и для прогноза по погоде.

При обучении ветер и температура берутся из SCADA турбины, при прогнозе — из
``frame.summary``: ветер на высоте ступицы и температура, усредненные по моделям погоды.
"""

import numpy as np
import pandas as pd

from ml_service.frame import Frame

FEATURES = ["ws", "t", "hour", "doy_sin", "doy_cos", "turbine"]
TURBINE_CODES = {"T1": 0, "T2": 1}
DESCRIPTIONS = {
    "ws": "Ветер на высоте ступицы, м/с",
    "t": "Температура воздуха, °C",
    "hour": "Час суток, UTC",
    "doy_sin": "Сезон: синус дня года",
    "doy_cos": "Сезон: косинус дня года",
    "turbine": "Турбина: 0 = T1, 1 = T2",
}


def build_features(ws, t, valid_time_utc, turbine: str) -> pd.DataFrame:
    times = pd.DatetimeIndex(valid_time_utc)
    day = 2 * np.pi * times.dayofyear.to_numpy() / 365.25
    return pd.DataFrame(
        {
            "ws": np.asarray(ws, dtype=float),
            "t": np.asarray(t, dtype=float),
            "hour": times.hour.to_numpy(),
            "doy_sin": np.sin(day),
            "doy_cos": np.cos(day),
            "turbine": TURBINE_CODES[turbine],
        },
        columns=FEATURES,
    )


def features_from_frame(frame: Frame, turbine: str) -> pd.DataFrame:
    summary = frame.summary
    return build_features(summary["wind_speed_hub_ms"], summary["t2m"], summary.index, turbine)
