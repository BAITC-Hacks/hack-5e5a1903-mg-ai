"""Сведения об источниках, которых нет в кэше: тип API, статус задержки, точность ветра, состав ансамбля.

Шаг прогонов, задержка и ее обоснование берутся из ``src.forecast.weather.sources``,
высоты, пустые колонки и период архива считаются по кэшу при старте.
"""

from typing import Literal

SourceKind = Literal["single_runs", "previous_runs", "ensemble"]

ENSEMBLE = "ensemble"
ENSEMBLE_MEMBERS = ("ifs025", "gfs", "icon", "gem")

KIND: dict[str, SourceKind] = {
    "ifs": "single_runs",
    "ifs025": "previous_runs",
    "gfs": "previous_runs",
    "icon": "previous_runs",
    "gem": "previous_runs",
}

# Задержки ifs и gfs приняты командой (#2, GOAL.md), остальные взяты с запасом без документированного времени публикации.
DELAY_CONFIRMED = {"ifs": True, "gfs": True, "ifs025": False, "icon": False, "gem": False}

# MAE ветра против SCADA, м/с, на отложенном периоде после 01.08.2025: d1 — заблаговременность +24…29 ч,
# d2 — +48…53 ч. Источник: reports/weather_vs_scada.md, раздел 3. IFS HRES в этом сравнении не участвовал.
WIND_MAE_MS: dict[str, dict[str, float] | None] = {
    "ifs": None,
    "ifs025": {"d1": 2.12, "d2": 2.32},
    "gfs": {"d1": 2.35, "d2": 2.57},
    "icon": {"d1": 1.97, "d2": 2.13},
    "gem": {"d1": 2.41, "d2": 2.55},
    ENSEMBLE: {"d1": 1.69, "d2": 1.87},
}

ENSEMBLE_TITLE = "Среднее четырех моделей Open-Meteo Previous Runs: ECMWF IFS 0.25°, GFS, ICON, GEM"
