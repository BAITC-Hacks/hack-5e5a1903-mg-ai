"""Настройки данных dev3: пути, часовой пояс SCADA, пороги флагов очистки."""

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def _dir_from_env(name: str, default: Path) -> Path:
    """Каталог из переменной окружения. Пустое значение означает каталог репозитория."""
    value = os.environ.get(name, "").strip()
    return Path(value) if value else default


DATA_DIR = _dir_from_env("DATA_DIR", REPO / "data")
REPORTS_DIR = _dir_from_env("REPORTS_DIR", REPO / "reports")

# Файлы SCADA в DATA_DIR. Имена с пробелами, как их выдали организаторы.
SCADA_FILES = {
    "T1": "Dataset HackAlemAI turbine 1.csv",
    "T2": "Dataset HackAlemAI turbine 2.csv",
}

# Правило часового пояса SCADA (#10): time_utc = время SCADA − 6 ч на всём периоде.
# Переход Алматинской области на UTC+5 01.03.2024 в данных не отражен,
# доказательство в reports/tz_check.md.
SCADA_UTC_OFFSET_H = 6

# Колонки ScadaHistory из контракта #2.
SCADA_COLUMNS = ("time_utc", "turbine", "power_norm", "wind_ms", "temp_c", "flag")

# Паспорт GW109/2500 из #2.
CUT_IN_MS = 3.0
RATED_MS = 10.3
CUT_OUT_MS = 25.0

# Час засчитывается, если в нем есть хотя бы 4 из 6 десятиминутных записей.
MIN_POINTS_PER_HOUR = 4

# Пороги флагов из #9. Мощность в долях номинала, ветер в м/с.
# В простое SCADA пишет мощность 0,01, а не 0, поэтому «около нуля» это до 0,02.
DOWNTIME_MAX_POWER = 0.02
DOWNTIME_WIND_MS = (4.0, 20.0)
CURTAILMENT_MAX_POWER = 0.95
CURTAILMENT_MIN_WIND_MS = 11.0
FROZEN_MIN_HOURS = 3
ICING_MAX_TEMP_C = 1.0
ICING_CURVE_SHARE = 0.5
