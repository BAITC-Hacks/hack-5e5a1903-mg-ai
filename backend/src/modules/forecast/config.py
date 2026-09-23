"""Факты кейса: объект, режим выпуска, источники погоды.

Это не параметры развертывания, а константы задачи, поэтому они лежат в коде,
а не в окружении. Из окружения приходят только адреса и таймауты соседних
сервисов, они в ``src/core/config.py``.

Константы вынесены в отдельный модуль, чтобы ими одинаково пользовались
и заглушка из ``service.py``, и проверки из ``analyze.py``, и цикл агента
из ``orchestrator.py``, без кольцевых импортов.
"""

from datetime import date, timedelta

from src.modules.forecast.schemas import Turbine

# Объект: ВЭС «Шелек», две турбины Goldwind GW109/2500.
TURBINES: tuple[Turbine, ...] = (
    Turbine(name="T1", lat=43.645150, lon=78.535604, rated_mw=2.5),
    Turbine(name="T2", lat=43.643198, lon=78.538828, rated_mw=2.5),
)
RATED_MW_PER_TURBINE = 2.5
NWP_POINT = (43.6442, 78.5372)
HUB_HEIGHT_M = 80
CUT_IN_MS = 3.0
RATED_MS = 10.3
CUT_OUT_MS = 25.0

# Выпуск в 07:00 по Астане накануне целевых суток, горизонт +1…+48 ч.
ISSUE_HOUR_UTC = 2
HORIZON_HOURS = 48
LOCAL_OFFSET = timedelta(hours=5)

# Ретро-симуляция: 28 выпусков февраля 2026.
FIRST_ISSUE = date(2026, 1, 31)
LAST_ISSUE = date(2026, 2, 27)

# Имена источников совпадают с реестром dev3 (``src/forecast/weather/sources.py``):
# по ним агент ходит в AsOfStore и их же отдает ML-сервису в поле ``source``.
SOURCE_IFS = "ifs"
SOURCE_IFS025 = "ifs025"
SOURCE_GFS = "gfs"
SOURCE_ICON = "icon"
SOURCE_GEM = "gem"
#: Готовый ансамбль сервиса погоды: усредняет модели сам, участников с ним не смешиваем.
SOURCE_ENSEMBLE = "ensemble"
#: Порядок по умолчанию: первым идет основной источник, остальные уточняют ансамбль.
#: Берется, когда паспорт модели не называет источники, на которых она обучена.
DEFAULT_SOURCES: tuple[str, ...] = (SOURCE_IFS025, SOURCE_GFS, SOURCE_ICON, SOURCE_GEM)
#: Шаг между прогонами: на него агент отступает назад, когда свежий прогон забракован.
RUN_CYCLE = timedelta(hours=6)
#: Модели погоды, которые показывает страница «Погода».
WEATHER_MODELS = ("ECMWF IFS", "GFS", "ICON", "GEM")


def rated_mw(turbine: str) -> float:
    """Номинал турбины по имени. Неизвестное имя считаем турбиной проекта."""
    for item in TURBINES:
        if item.name == turbine:
            return item.rated_mw
    return RATED_MW_PER_TURBINE
