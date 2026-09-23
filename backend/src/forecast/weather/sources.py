"""Реестр источников прогноза погоды: шаг прогонов, задержка публикации, папка кэша.

Временно живет в коде: после #5 реестр переедет в конфиг пайплайна, а этот модуль
будет только читать его.

Задержка публикации отсчитывается от времени запуска прогона: прогон считается
доступным в момент ``run_init_utc + delay``. Значение выбирается консервативно,
то есть не меньше самого позднего реального времени публикации за всю историю.
Ошибка в большую сторону стоит немного точности, в меньшую это утечка будущего.
"""

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True)
class Source:
    name: str
    title: str
    run_step_h: int
    delay: timedelta
    cache_dir: str
    delay_basis: str


SOURCES: dict[str, Source] = {
    source.name: source
    for source in (
        Source(
            name="ifs",
            title="ECMWF IFS HRES 9 km, Open-Meteo Single Runs",
            run_step_h=6,
            delay=timedelta(hours=7, minutes=30),
            cache_dir="ifs",
            delay_basis="Решение команды (#2, GOAL.md). По метаданным Open-Meteo прогон 00z 23.09.2026 стал доступен через 7.0 ч.",
        ),
        Source(
            name="ifs025",
            title="ECMWF IFS 0.25°, Open-Meteo Previous Runs",
            run_step_h=6,
            delay=timedelta(hours=10),
            cache_dir="ifs025",
            delay_basis=(
                "Расписание ECMWF: последний шаг прогонов 00z и 12z рассылается в 07:34 после запуска. "
                "Open data шли с дополнительной задержкой к этому расписанию, Open-Meteo пишет о 2 ч; без задержки ECMWF отдает их с 01.10.2025. "
                "Для истории 2024-2025 это до 9:34, округлено вверх до 10 ч. "
                "По метаданным Open-Meteo прогон 00z 23.09.2026 стал доступен через 7.8 ч, то есть 8 ч здесь мало."
            ),
        ),
        Source(
            name="gfs",
            title="NOAA GFS, Open-Meteo Previous Runs",
            run_step_h=6,
            delay=timedelta(hours=7),
            cache_dir="gfs",
            delay_basis="Решение команды (#2, GOAL.md). По метаданным Open-Meteo прогон 00z 23.09.2026 стал доступен через 6.5 ч.",
        ),
        Source(
            name="icon",
            title="DWD ICON global, Open-Meteo Previous Runs",
            run_step_h=6,
            delay=timedelta(hours=8),
            cache_dir="icon",
            delay_basis=(
                "Допущение. Документированного времени публикации у DWD не нашли. "
                "Сторонние загрузчики пишут «до 5 ч, обычно 2-3 ч», по метаданным Open-Meteo прогон 06z 23.09.2026 доступен через 3.8 ч. "
                "Взяли 8 ч с запасом."
            ),
        ),
        Source(
            name="gem",
            title="ECCC GEM global (GDPS), Open-Meteo Previous Runs",
            run_step_h=12,
            delay=timedelta(hours=8),
            cache_dir="gem",
            delay_basis=(
                "Допущение. В документации ECCC Datamart время публикации GDPS не указано, метаданные Open-Meteo по GEM устарели. "
                "Взяли 8 ч с запасом. Прогоны только 00z и 12z."
            ),
        ),
    )
}


def get_source(name: str) -> Source:
    try:
        return SOURCES[name]
    except KeyError:
        raise ValueError(f"Неизвестный источник погоды {name!r}, есть: {', '.join(SOURCES)}") from None
