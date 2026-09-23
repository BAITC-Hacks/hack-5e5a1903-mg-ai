"""Загрузчик архивных прогнозов четырех моделей из Open-Meteo Previous Runs API.

Работает онлайн и нужен только для обновления кэша: обычный запуск проекта читает
закоммиченный кэш и сеть не трогает. Запуск из папки backend:

    uv run python -m src.forecast.weather.fetch_prev_runs                 # все модели
    uv run python -m src.forecast.weather.fetch_prev_runs --models gem    # одна модель

Previous Runs отдает не прогон целиком, а для каждого часа t значения
``X_previous_dayN`` — что предсказывал прогон, запущенный примерно за N суток до t.
Какой это прогон, считает ``prev_runs_rule.run_init_for``. У моделей с шагом данных
3 ч промежуточные часы интерполированы по соседним точкам из разных прогонов,
поэтому ``run_init_utc`` в кэше — самый новый прогон, чьи значения вошли в час.

Кэш: ``<DATA_DIR>/nwp/<source>/<yyyy>.csv.gz`` по году ``valid_time_utc`` и
``<DATA_DIR>/nwp/<source>/SHA256SUMS``. Формат длинный, общий для всех источников:
одна строка на пару (``run_init_utc``, ``valid_time_utc``), затем ``prev_day``
для прослеживаемости и колонки ``VARIABLES`` с именами как в API. Переменные,
которых у модели нет, остаются пустыми колонками. Строки, где нет ветра ни на
одной высоте, в кэш не попадают. Файлы детерминированы: сортировка, gzip с
mtime=0, числа ``%.2f``, поэтому повторная загрузка тех же данных не меняет хэши.
"""

import argparse
import gzip
import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import reduce
from operator import and_
from pathlib import Path

import httpx
import pandas as pd

from src.forecast.weather.prev_runs_rule import PREV_DAYS, run_init_for

logger = logging.getLogger(__name__)

URL = "https://previous-runs-api.open-meteo.com/v1/forecast"
# Середина между турбинами: они в ~330 м друг от друга, это одна ячейка сетки.
LAT, LON = 43.6442, 78.5372
# Конец периода — 01.03.2026: последний выпуск ретро-симуляции 27.02 в 02:00 UTC
# с горизонтом +48 ч заканчивается 01.03 в 02:00.
START, END = date(2024, 2, 15), date(2026, 3, 1)

# Общий набор колонок кэша, в этом порядке. Направление у GEM только на 80 м:
# на 100 м эта модель ничего не отдает.
VARIABLES = (
    "wind_speed_80m",
    "wind_speed_100m",
    "wind_speed_120m",
    "wind_direction_80m",
    "wind_direction_100m",
    "wind_gusts_10m",
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
)
COLUMNS = ("run_init_utc", "valid_time_utc", "prev_day", *VARIABLES)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

REPO = Path(__file__).resolve().parents[4]

TIMEOUT_S = float(os.environ.get("NWP_HTTP_TIMEOUT_S") or 120)
ATTEMPTS = 4
BACKOFF_S = 2.0
# Open-Meteo без ключа: 600 вызовов в минуту. Запрос больше чем с 10 переменными
# или длиннее двух недель считается за несколько вызовов.
CALLS_PER_MINUTE = 600
RATE_LIMIT_PAUSE_S = 60.0
# Доля часов, где previous_dayN совпадает со свежим прогоном сразу по всем
# переменным проверки. Выше порога архив, похоже, заполнен не старым прогоном.
QC_WARN_SHARE = 0.01


@dataclass(frozen=True)
class PrevRunsModel:
    """Модель в Previous Runs и что она реально отдает с суффиксом previous_dayN."""

    source: str
    api_model: str
    cycle_h: int
    variables: tuple[str, ...]
    # Шаг данных модели у Open-Meteo, ``temporal_resolution_seconds`` в метаданных.
    data_step_h: int = 1

    @property
    def wind_speeds(self) -> tuple[str, ...]:
        return tuple(v for v in self.variables if v.startswith("wind_speed_"))

    @property
    def qc_variables(self) -> tuple[str, ...]:
        """Переменные, по которым previous_dayN сверяется со свежим прогоном."""
        return (self.wind_speeds[0], "temperature_2m", "surface_pressure")


_SURFACE = ("wind_gusts_10m", "temperature_2m", "relative_humidity_2m", "surface_pressure")
_WIND_3 = ("wind_speed_80m", "wind_speed_100m", "wind_speed_120m", "wind_direction_100m")

# Проверено пробным запросом: у ecmwf_ifs025 ветер только на 100 м и нет порывов,
# влажность с 04.03.2024; у gem_global нет ни скорости, ни направления на 100 м.
# Шаг данных 3 ч у ecmwf_ifs025 и gem_global, 1 ч у gfs_global и icon_global.
MODELS: dict[str, PrevRunsModel] = {
    "ifs025": PrevRunsModel(
        "ifs025",
        "ecmwf_ifs025",
        6,
        ("wind_speed_100m", "wind_direction_100m", "temperature_2m", "relative_humidity_2m", "surface_pressure"),
        data_step_h=3,
    ),
    "gfs": PrevRunsModel("gfs", "gfs_global", 6, _WIND_3 + _SURFACE),
    "icon": PrevRunsModel("icon", "icon_global", 6, _WIND_3 + _SURFACE),
    "gem": PrevRunsModel("gem", "gem_global", 12, ("wind_speed_80m", "wind_speed_120m", "wind_direction_80m", *_SURFACE), data_step_h=3),
}


class FetchError(RuntimeError):
    """Open-Meteo не ответил или ответил не тем, что ожидалось."""


def nwp_dir() -> Path:
    """Каталог кэша погоды: ``DATA_DIR/nwp``, по умолчанию ``data/nwp`` в репозитории."""
    return Path(os.environ.get("DATA_DIR") or REPO / "data") / "nwp"


def request_variables(model: PrevRunsModel) -> list[str]:
    """Переменные запроса: previous_day1..3 плюс свежий прогон для проверки качества."""
    return [f"{v}_previous_day{n}" for v in model.variables for n in PREV_DAYS] + list(model.qc_variables)


def request_weight(n_variables: int, start: date, end: date) -> float:
    """Сколько вызовов из лимита Open-Meteo стоит один запрос."""
    days = (end - start).days + 1
    return max(1.0, n_variables / 10) * max(1.0, days / 14)


def year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    return [(max(start, date(y, 1, 1)), min(end, date(y, 12, 31))) for y in range(start.year, end.year + 1)]


def get_json(client: httpx.Client, params: dict, *, attempts: int = ATTEMPTS, sleep: Callable[[float], None] = time.sleep) -> dict:
    """GET с повторами на сетевых ошибках, 429, 5xx и ответе 200 не в JSON. Остальные ошибки сразу в ``FetchError``.

    Ответ 200 не в JSON Open-Meteo отдает, когда сбой случился посреди потока:
    тело тогда — текст вида ``Unexpected error while streaming data: …``.
    """
    error = ""
    for attempt in range(attempts):
        pause = BACKOFF_S * 2**attempt
        try:
            response = client.get(URL, params=params)
        except httpx.TransportError as exc:
            error = f"сеть: {exc!r}"
        else:
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("hourly"), dict):
                    return payload
            error = f"HTTP {response.status_code}: {response.text[:300]}"
            if response.status_code == 200:
                error = f"ответ без блока hourly, {error}"
            elif response.status_code == 429:
                pause = RATE_LIMIT_PAUSE_S
            elif response.status_code < 500:
                raise FetchError(f"Open-Meteo отклонил запрос, {error}")
        if attempt + 1 < attempts:
            logger.warning("попытка %d из %d не удалась (%s), пауза %.0f с", attempt + 1, attempts, error, pause)
            sleep(pause)
    raise FetchError(f"Open-Meteo не ответил за {attempts} попыток, последняя ошибка: {error}")


def _series(hourly: dict, key: str) -> pd.Series:
    if key not in hourly:
        raise FetchError(f"в ответе нет переменной {key}")
    return pd.Series(hourly[key], dtype="float64")


def _valid_times(payload: dict) -> pd.DatetimeIndex:
    if payload.get("utc_offset_seconds", 0) != 0:
        raise FetchError(f"ожидалось время в UTC, пришло смещение {payload['utc_offset_seconds']} с")
    return pd.DatetimeIndex(pd.to_datetime(payload["hourly"]["time"])).tz_localize("UTC")


def to_long(payload: dict, model: PrevRunsModel) -> pd.DataFrame:
    """Ответ API в длинный формат кэша: строка на (прогон, час), колонки ``COLUMNS``."""
    hourly = payload["hourly"]
    valid = _valid_times(payload)
    parts = []
    for n in PREV_DAYS:
        run_init = run_init_for(valid, n, model.cycle_h, model.data_step_h)
        part = pd.DataFrame({"run_init_utc": run_init, "valid_time_utc": valid, "prev_day": n})
        for var in VARIABLES:
            part[var] = _series(hourly, f"{var}_previous_day{n}").to_numpy() if var in model.variables else float("nan")
        parts.append(part)
    frame = pd.concat(parts, ignore_index=True).dropna(subset=list(model.wind_speeds), how="all")
    return frame.sort_values(["run_init_utc", "valid_time_utc"]).reset_index(drop=True)[list(COLUMNS)]


def qc_counts(payload: dict, model: PrevRunsModel) -> dict[str, int]:
    """Сколько часов previous_dayN подозрительно совпадает со свежим прогоном.

    Совпадение по одной переменной бывает случайным: GEM округляет значения
    до 0.1, и прогноз на сутки вперед нередко попадает в то же значение, что
    и свежий. Поэтому отдельно считается совпадение сразу по всем
    ``qc_variables``: оно и означает, что архив заполнен свежим прогоном.
    """
    hourly = payload["hourly"]
    fresh = {v: _series(hourly, v) for v in model.qc_variables}
    days = {n: {v: _series(hourly, f"{v}_previous_day{n}") for v in model.qc_variables} for n in PREV_DAYS}
    present = days[1][model.wind_speeds[0]].notna()
    counts = {"hours": int(present.sum())}
    for n in PREV_DAYS:
        same = {v: days[n][v] == fresh[v] for v in model.qc_variables}
        counts[f"day{n}_same_wind"] = int((same[model.wind_speeds[0]] & present).sum())
        counts[f"day{n}_same_all"] = int((reduce(and_, same.values()) & present).sum())
    all_days = [days[1][v].eq(days[2][v]) & days[2][v].eq(days[3][v]) for v in model.qc_variables]
    counts["all_days_same_wind"] = int((all_days[0] & present).sum())
    counts["all_days_same_all"] = int((reduce(and_, all_days) & present).sum())
    return counts


def write_cache(frame: pd.DataFrame, source: str) -> list[Path]:
    """Пишет кэш по годам ``valid_time_utc`` и пересчитывает ``SHA256SUMS`` источника."""
    out_dir = nwp_dir() / source
    out_dir.mkdir(parents=True, exist_ok=True)
    frame = frame.sort_values(["run_init_utc", "valid_time_utc"])
    paths = []
    for year, part in frame.groupby(frame["valid_time_utc"].dt.year):
        text = part.to_csv(index=False, float_format="%.2f", date_format=TIME_FORMAT, lineterminator="\n")
        path = out_dir / f"{year}.csv.gz"
        path.write_bytes(gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0))
        paths.append(path)
    write_checksums(out_dir)
    return paths


def write_checksums(out_dir: Path) -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in sorted(out_dir.glob("*.csv.gz"))]
    (out_dir / "SHA256SUMS").write_bytes("".join(lines).encode("utf-8"))


def read_checksums(source: str) -> dict[str, str]:
    """Имя файла -> ожидаемый SHA-256 из ``SHA256SUMS``."""
    text = (nwp_dir() / source / "SHA256SUMS").read_text(encoding="utf-8")
    return {name: digest for digest, name in (line.split(maxsplit=1) for line in text.splitlines() if line.strip())}


def read_cache(source: str) -> pd.DataFrame:
    """Весь кэш источника одной таблицей, время в UTC."""
    paths = sorted((nwp_dir() / source).glob("*.csv.gz"))
    if not paths:
        raise FileNotFoundError(f"нет кэша {nwp_dir() / source}, запусти fetch_prev_runs")
    frame = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    for col in ("run_init_utc", "valid_time_utc"):
        frame[col] = pd.to_datetime(frame[col], format=TIME_FORMAT, utc=True)
    return frame.sort_values(["run_init_utc", "valid_time_utc"]).reset_index(drop=True)


def fetch_model(
    model: PrevRunsModel,
    client: httpx.Client,
    *,
    start: date = START,
    end: date = END,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Один запрос на год, затем длинный формат и счетчики проверки качества."""
    variables = request_variables(model)
    frames, qc = [], {}
    for lo, hi in year_chunks(start, end):
        params = {
            "latitude": LAT,
            "longitude": LON,
            "start_date": lo.isoformat(),
            "end_date": hi.isoformat(),
            "hourly": ",".join(variables),
            "models": model.api_model,
            "wind_speed_unit": "ms",
            "timezone": "GMT",
        }
        payload = get_json(client, params, sleep=sleep)
        frames.append(to_long(payload, model))
        for key, value in qc_counts(payload, model).items():
            qc[key] = qc.get(key, 0) + value
        logger.info("%s %s…%s: %d строк", model.source, lo, hi, len(frames[-1]))
        sleep(request_weight(len(variables), lo, hi) * 60 / CALLS_PER_MINUTE)
    frame = pd.concat(frames, ignore_index=True)
    empty = [v for v in model.variables if frame[v].isna().all()]
    if empty:
        raise FetchError(f"{model.source}: за весь период пустые переменные {empty}")
    return frame, qc


def log_qc(model: PrevRunsModel, qc: dict[str, int]) -> None:
    hours = max(qc["hours"], 1)
    for n in PREV_DAYS:
        logger.info(
            "%s previous_day%d совпадает со свежим прогоном: по ветру %.2f%%, по %s сразу %.3f%% (%d ч из %d)",
            model.source,
            n,
            100 * qc[f"day{n}_same_wind"] / hours,
            "+".join(model.qc_variables),
            100 * qc[f"day{n}_same_all"] / hours,
            qc[f"day{n}_same_all"],
            qc["hours"],
        )
    logger.info(
        "%s previous_day1..3 одинаковы: по ветру %.2f%%, по всем переменным проверки %.3f%% (%d ч)",
        model.source,
        100 * qc["all_days_same_wind"] / hours,
        100 * qc["all_days_same_all"] / hours,
        qc["all_days_same_all"],
    )
    worst = max(qc[f"day{n}_same_all"] for n in PREV_DAYS) / hours
    if worst > QC_WARN_SHARE or qc["all_days_same_all"] / hours > QC_WARN_SHARE:
        logger.warning("%s: доля часов с копией свежего прогона выше %.0f%%, архив нужно разобрать", model.source, 100 * QC_WARN_SHARE)


def _span(frame: pd.DataFrame) -> tuple[str, str]:
    valid = frame["valid_time_utc"]
    return f"{valid.min():%Y-%m-%d %H:%M}", f"{valid.max():%Y-%m-%d %H:%M}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", choices=sorted(MODELS), default=list(MODELS), help="какие источники загрузить")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=15.0)) as client:
        for source in args.models:
            model = MODELS[source]
            frame, qc = fetch_model(model, client)
            paths = write_cache(frame, source)
            log_qc(model, qc)
            size = sum(p.stat().st_size for p in paths)
            logger.info("%s: %d строк, %s…%s, %.1f МБ в %s", source, len(frame), *_span(frame), size / 2**20, paths[0].parent)


if __name__ == "__main__":
    main()
