"""Загрузчик архивных прогонов ECMWF IFS HRES 9 км из Open-Meteo Single Runs API.

Single Runs отдает конкретный прогон по времени запуска, поэтому для каждого значения
известно, от какого прогона оно взято. Это то, что нужно для честного as-of: время
доступности прогона считается дальше как запуск плюс задержка публикации (#7).

Работает онлайн и нужен только для пополнения кэша. Обычный запуск проекта читает
закоммиченный кэш и сеть не трогает.

Запуск из папки backend:

    uv run python -m src.forecast.weather.fetch_ifs                    # весь период
    uv run python -m src.forecast.weather.fetch_ifs --start 2024-03-15 --end 2024-03-17

Какие прогоны качаются: 12z и 18z каждого дня для обучения, а с 01.02.2026 все четыре
прогона суток для пересчетов в ретро-симуляции февраля. Повторный запуск докачивает
только то, чего нет в кэше.

Формат кэша общий для всех моделей погоды: ``data/nwp/<source>/<yyyy-mm>.csv.gz``,
одна строка на час прогона, колонки ``run_init_utc``, ``valid_time_utc`` и переменные
с именами как в API. Месяц файла — месяц запуска прогона. Файл пишется детерминированно,
поэтому повторный запуск дает побайтно тот же файл, а хэши лежат в ``SHA256SUMS``.
Недоступные прогоны и пустые значения перечислены в ``gaps.csv``.
"""

import argparse
import gzip
import hashlib
import logging
import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from src.forecast.weather.sources import get_source

logger = logging.getLogger(__name__)

API_URL = "https://single-runs-api.open-meteo.com/v1/forecast"
MODEL = "ecmwf_ifs"
SOURCE = get_source("ifs")

# Середина между T1 и T2, до каждой около 170 м, это одна ячейка сетки 9 км.
LAT, LON = 43.6442, 78.5372
HOURS = 72
VARIABLES = (
    "wind_speed_80m",
    "wind_speed_100m",
    "wind_speed_120m",
    "wind_direction_100m",
    "wind_gusts_10m",
    "temperature_2m",
    "relative_humidity_2m",
    "surface_pressure",
)
COLUMNS = ("run_init_utc", "valid_time_utc", *VARIABLES)

# Порыв — максимум за прошедший час, у нулевого часа прогона его нет по определению.
# Такие пустые значения ожидаемы и в gaps.csv не попадают.
STRUCTURAL_GAPS: dict[str, frozenset[int]] = {"wind_gusts_10m": frozenset({0})}

# Архив IFS 9 км в Single Runs начинается 14.03.2024, первый полный день — 15.03.
ARCHIVE_START = date(2024, 3, 15)
ARCHIVE_END = date(2026, 2, 28)
TRAIN_RUN_HOURS = (12, 18)
# С этого дня нужны все прогоны: по ним агент пересчитывает февральские выпуски.
ALL_RUNS_FROM = date(2026, 2, 1)
ALL_RUN_HOURS = (0, 6, 12, 18)

# Тот же вид времени, что в кэше Previous Runs: формат общий для всех источников.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
FLOAT_FORMAT = "%.2f"
GAPS_FILE = "gaps.csv"
GAPS_COLUMNS = ("run_init_utc", "variable", "lead_hours", "reason")
SUMS_FILE = "SHA256SUMS"
UNAVAILABLE = "modelRunUnavailable"

# Лимит Open-Meteo без ключа: 600 запросов в минуту, 5 000 в час, 10 000 в сутки.
# Пауза 0,8 с держит нас ниже часового лимита с запасом.
DEFAULT_MIN_INTERVAL_S = 0.8
# Таймаут запроса берется из NWP_HTTP_TIMEOUT_S, как у загрузчика Previous Runs.
DEFAULT_TIMEOUT_S = 120.0
DEFAULT_RETRIES = 5
BACKOFF_S = 2.0
RATE_LIMIT_PAUSE_S = 60.0


class FetchError(RuntimeError):
    """Загрузка не может продолжаться: сеть, лимит или неожиданный ответ API."""


class RunUnavailable(FetchError):
    """Прогона нет в архиве. Загрузка его пропускает и записывает в gaps.csv."""


def default_cache_dir() -> Path:
    """``$DATA_DIR/nwp/ifs``, а без переменной — ``data/nwp/ifs`` в корне репозитория."""
    data_dir = os.environ.get("DATA_DIR") or Path(__file__).resolve().parents[4] / "data"
    return Path(data_dir) / "nwp" / SOURCE.cache_dir


def planned_runs(start: date, end: date) -> list[datetime]:
    """Время запуска всех прогонов, которые нужны за дни [start, end]."""
    runs = []
    day = start
    while day <= end:
        hours = ALL_RUN_HOURS if day >= ALL_RUNS_FROM else TRAIN_RUN_HOURS
        runs.extend(datetime(day.year, day.month, day.day, hour, tzinfo=UTC) for hour in hours)
        day += timedelta(days=1)
    return runs


# ---------------------------------------------------------------- запросы


class RateLimiter:
    """Выдерживает минимальную паузу между запросами одного запуска загрузчика."""

    def __init__(self, min_interval_s: float, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            delay = self._last + self.min_interval_s - now
            if delay > 0:
                self._sleep(delay)
                now += delay
        self._last = now


def run_params(run: datetime) -> dict[str, str | float | int]:
    return {
        "latitude": LAT,
        "longitude": LON,
        "models": MODEL,
        "run": run.strftime("%Y-%m-%dT%H:%M"),
        "hourly": ",".join(VARIABLES),
        "forecast_hours": HOURS,
        "wind_speed_unit": "ms",
        "timezone": "GMT",
    }


def _reason(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    if isinstance(payload, dict) and payload.get("reason"):
        return str(payload["reason"])
    return response.text[:300]


def request_run(
    client: httpx.Client,
    run: datetime,
    limiter: RateLimiter,
    retries: int = DEFAULT_RETRIES,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Ответ API на один прогон.

    Сеть, 5xx и 429 повторяются с растущей паузой. Недоступный прогон сразу дает
    ``RunUnavailable``, прочие ошибки 4xx и исчерпанный дневной лимит — ``FetchError``:
    повтор тут не поможет.
    """
    label = run.strftime(TIME_FORMAT)
    error = ""
    for attempt in range(1, retries + 1):
        limiter.wait()
        pause = BACKOFF_S * 2 ** (attempt - 1)
        try:
            response = client.get(API_URL, params=run_params(run))
        except httpx.TransportError as exc:
            error = f"сеть: {type(exc).__name__}: {exc}"
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise FetchError(f"прогон {label}: ответ не JSON: {response.text[:200]}") from exc
            reason = _reason(response)
            if response.status_code == 400 and "not available" in reason.lower():
                raise RunUnavailable(reason)
            if response.status_code == 429:
                if "daily" in reason.lower():
                    raise FetchError(f"исчерпан дневной лимит Open-Meteo: {reason}. Запустите позже, загрузка продолжится с места остановки")
                error = f"429: {reason}"
                pause = max(pause, RATE_LIMIT_PAUSE_S)
            elif response.status_code >= 500:
                error = f"{response.status_code}: {reason}"
            else:
                raise FetchError(f"прогон {label}: Open-Meteo {response.status_code}: {reason}")
        if attempt < retries:
            logger.warning("прогон %s, попытка %d из %d: %s, повтор через %.0f с", label, attempt, retries, error, pause)
            sleep(pause)
    raise FetchError(f"прогон {label}: Open-Meteo не ответил после {retries} попыток, последняя ошибка — {error}")


def parse_run(payload: dict, run: datetime) -> pd.DataFrame:
    """Ответ API -> ровно HOURS строк прогона в длинном формате.

    Недостающие часы и ``null`` становятся NaN: в кэше у прогона всегда все часы,
    а пустые значения потом попадают в gaps.csv.
    """
    label = run.strftime(TIME_FORMAT)
    if payload.get("error"):
        raise FetchError(f"прогон {label}: {payload.get('reason')}")
    if payload.get("utc_offset_seconds", 0) != 0:
        raise FetchError(f"прогон {label}: время ответа не в UTC, utc_offset_seconds={payload['utc_offset_seconds']}")
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict) or "time" not in hourly:
        raise FetchError(f"прогон {label}: в ответе нет блока hourly")
    absent = [name for name in VARIABLES if name not in hourly]
    if absent:
        raise FetchError(f"прогон {label}: в ответе нет переменных {', '.join(absent)}")

    frame = pd.DataFrame({name: pd.to_numeric(pd.Series(hourly[name], dtype=object), errors="coerce") for name in VARIABLES})
    frame.index = pd.to_datetime(pd.Series(hourly["time"]), format="%Y-%m-%dT%H:%M").dt.tz_localize("UTC")
    frame = frame[~frame.index.duplicated()]
    expected = pd.date_range(run, periods=HOURS, freq="h", name="valid_time_utc")
    frame = frame.reindex(expected).reset_index()
    frame.insert(0, "run_init_utc", pd.Timestamp(run))
    return frame.astype({name: "float64" for name in VARIABLES})


def find_gaps(frame: pd.DataFrame) -> list[dict[str, str]]:
    """Пустые значения прогонов, кроме ожидаемых из STRUCTURAL_GAPS, строками gaps.csv."""
    rows = []
    for run, part in frame.groupby("run_init_utc", sort=True):
        leads = ((part["valid_time_utc"] - run) // pd.Timedelta(hours=1)).to_numpy()
        for name in VARIABLES:
            missing = sorted(int(lead) for lead in leads[part[name].isna().to_numpy()] if lead not in STRUCTURAL_GAPS.get(name, ()))
            if missing:
                rows.append(
                    {"run_init_utc": run.strftime(TIME_FORMAT), "variable": name, "lead_hours": ";".join(map(str, missing)), "reason": "null"}
                )
    return rows


# ---------------------------------------------------------------- кэш


def month_path(cache_dir: Path, month: str) -> Path:
    return cache_dir / f"{month}.csv.gz"


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def serialize_month(frame: pd.DataFrame) -> bytes:
    """Детерминированный gzip: порядок строк, формат чисел и времени, mtime=0, без имени файла."""
    ordered = frame.sort_values(["run_init_utc", "valid_time_utc"]).loc[:, list(COLUMNS)].copy()
    for column in ("run_init_utc", "valid_time_utc"):
        ordered[column] = ordered[column].dt.strftime(TIME_FORMAT)
    text = ordered.to_csv(index=False, float_format=FLOAT_FORMAT, lineterminator="\n")
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def read_month(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={name: "float64" for name in VARIABLES})
    if list(frame.columns) != list(COLUMNS):
        raise FetchError(f"{path.name}: колонки {list(frame.columns)}, ожидались {list(COLUMNS)}")
    for column in ("run_init_utc", "valid_time_utc"):
        frame[column] = pd.to_datetime(frame[column], format=TIME_FORMAT, utc=True)
    return frame


def month_files(cache_dir: Path) -> list[Path]:
    return sorted(cache_dir.glob("*.csv.gz"))


def load_runs(cache_dir: Path | None = None) -> pd.DataFrame:
    """Весь кэш одной таблицей, отсортированной по (run_init_utc, valid_time_utc)."""
    files = month_files(cache_dir or default_cache_dir())
    if not files:
        return pd.DataFrame({column: pd.Series(dtype="datetime64[ns, UTC]" if column.endswith("_utc") else "float64") for column in COLUMNS})
    frames = [read_month(path) for path in files]
    return pd.concat(frames, ignore_index=True).sort_values(["run_init_utc", "valid_time_utc"], ignore_index=True)


def save_month(cache_dir: Path, month: str, new: list[pd.DataFrame]) -> None:
    """Добавляет прогоны в файл месяца. Прогон, который уже был, заменяется новым."""
    path = month_path(cache_dir, month)
    parts = [read_month(path)] if path.exists() else []
    parts.extend(new)
    merged = pd.concat(parts, ignore_index=True).drop_duplicates(["run_init_utc", "valid_time_utc"], keep="last")
    _atomic_write(path, serialize_month(merged))


def read_gaps(cache_dir: Path) -> pd.DataFrame:
    path = cache_dir / GAPS_FILE
    if not path.exists():
        return pd.DataFrame(columns=list(GAPS_COLUMNS), dtype=str)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def save_gaps(cache_dir: Path, refetched: Iterable[datetime], rows: list[dict[str, str]]) -> None:
    """Заменяет записи о перекачанных прогонах новыми и пишет gaps.csv."""
    refetched_labels = {run.strftime(TIME_FORMAT) for run in refetched}
    gaps = read_gaps(cache_dir)
    gaps = gaps[~gaps["run_init_utc"].isin(refetched_labels)]
    gaps = pd.concat([gaps, pd.DataFrame(rows, columns=list(GAPS_COLUMNS))], ignore_index=True)
    gaps = gaps.sort_values(["run_init_utc", "variable"], ignore_index=True)
    _atomic_write(cache_dir / GAPS_FILE, gaps.to_csv(index=False, lineterminator="\n").encode("utf-8"))


def unavailable_runs(cache_dir: Path) -> set[datetime]:
    gaps = read_gaps(cache_dir)
    labels = gaps.loc[gaps["reason"] == UNAVAILABLE, "run_init_utc"]
    return {datetime.strptime(label, TIME_FORMAT).replace(tzinfo=UTC) for label in labels}


def cached_runs(cache_dir: Path) -> set[datetime]:
    runs: set[datetime] = set()
    for path in month_files(cache_dir):
        column = pd.read_csv(path, usecols=["run_init_utc"])["run_init_utc"].unique()
        runs.update(datetime.strptime(label, TIME_FORMAT).replace(tzinfo=UTC) for label in column)
    return runs


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashed_files(cache_dir: Path) -> list[Path]:
    gaps = cache_dir / GAPS_FILE
    return month_files(cache_dir) + ([gaps] if gaps.exists() else [])


def write_sums(cache_dir: Path) -> None:
    lines = [f"{_sha256(path)}  {path.name}\n" for path in hashed_files(cache_dir)]
    _atomic_write(cache_dir / SUMS_FILE, "".join(lines).encode("utf-8"))


def verify_sums(cache_dir: Path) -> list[str]:
    """Расхождения между файлами кэша и SHA256SUMS. Пустой список — всё сходится."""
    sums_path = cache_dir / SUMS_FILE
    if not sums_path.exists():
        return [f"нет файла {SUMS_FILE}"]
    expected = {}
    for line in sums_path.read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        expected[name] = digest
    actual = {path.name: path for path in hashed_files(cache_dir)}
    problems = [f"{name}: указан в {SUMS_FILE}, но файла нет" for name in sorted(expected.keys() - actual.keys())]
    problems += [f"{name}: файла нет в {SUMS_FILE}" for name in sorted(actual.keys() - expected.keys())]
    problems += [f"{name}: хэш не совпадает" for name in sorted(expected.keys() & actual.keys()) if _sha256(actual[name]) != expected[name]]
    return problems


# ---------------------------------------------------------------- загрузка


@dataclass
class FetchSummary:
    planned: int = 0
    already_cached: int = 0
    downloaded: int = 0
    unavailable: int = 0


def fetch(
    start: date,
    end: date,
    cache_dir: Path,
    client: httpx.Client,
    limiter: RateLimiter,
    retries: int = DEFAULT_RETRIES,
    retry_unavailable: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> FetchSummary:
    """Докачивает недостающие прогоны за [start, end] в кэш.

    Файл месяца дописывается, как только его прогоны обработаны, а также при любой
    ошибке. Прерванная загрузка теряет не больше одного месяца запросов.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    runs = planned_runs(start, end)
    done = cached_runs(cache_dir)
    skipped = set() if retry_unavailable else unavailable_runs(cache_dir)
    todo = [run for run in runs if run not in done and run not in skipped]
    summary = FetchSummary(planned=len(runs), already_cached=len(runs) - len(todo))
    logger.info("прогонов в плане: %d, уже в кэше или известно, что недоступны: %d, скачать: %d", len(runs), summary.already_cached, len(todo))

    by_month: dict[str, list[datetime]] = {}
    for run in todo:
        by_month.setdefault(run.strftime("%Y-%m"), []).append(run)

    try:
        for month, month_runs in by_month.items():
            frames: list[pd.DataFrame] = []
            gaps: list[dict[str, str]] = []
            processed: list[datetime] = []
            try:
                for run in month_runs:
                    try:
                        frame = parse_run(request_run(client, run, limiter, retries=retries, sleep=sleep), run)
                    except RunUnavailable as exc:
                        logger.warning("прогон %s недоступен, пропускаю: %s", run.strftime(TIME_FORMAT), exc)
                        gaps.append(
                            {"run_init_utc": run.strftime(TIME_FORMAT), "variable": "*", "lead_hours": f"0-{HOURS - 1}", "reason": UNAVAILABLE}
                        )
                        summary.unavailable += 1
                    else:
                        frames.append(frame)
                        gaps.extend(find_gaps(frame))
                        summary.downloaded += 1
                    processed.append(run)
            finally:
                if frames:
                    save_month(cache_dir, month, frames)
                if processed:
                    save_gaps(cache_dir, processed, gaps)
                logger.info("%s: скачано %d, недоступно %d", month, len(frames), len(processed) - len(frames))
    finally:
        write_sums(cache_dir)
    return summary


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"дата в формате YYYY-MM-DD, получено {value!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Докачать архивные прогоны ECMWF IFS HRES 9 км из Open-Meteo Single Runs в кэш.")
    parser.add_argument("--start", type=_parse_date, default=ARCHIVE_START, help=f"первый день, по умолчанию {ARCHIVE_START}")
    parser.add_argument("--end", type=_parse_date, default=ARCHIVE_END, help=f"последний день включительно, по умолчанию {ARCHIVE_END}")
    parser.add_argument("--cache-dir", type=Path, default=None, help="каталог кэша, по умолчанию $DATA_DIR/nwp/ifs")
    timeout_s = float(os.environ.get("NWP_HTTP_TIMEOUT_S") or DEFAULT_TIMEOUT_S)
    parser.add_argument("--timeout", type=float, default=timeout_s, help="таймаут одного запроса, с, по умолчанию $NWP_HTTP_TIMEOUT_S или 120")
    parser.add_argument("--min-interval", type=float, default=DEFAULT_MIN_INTERVAL_S, help="минимальная пауза между запросами, с")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="попыток на один прогон")
    parser.add_argument("--retry-unavailable", action="store_true", help="еще раз запросить прогоны, которые раньше были недоступны")
    args = parser.parse_args(argv)
    if args.start > args.end:
        parser.error("--start позже --end")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx пишет каждый запрос на INFO, в полторы тысячи строк прогресс не разглядеть
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cache_dir = args.cache_dir or default_cache_dir()
    limiter = RateLimiter(args.min_interval)
    try:
        with httpx.Client(timeout=httpx.Timeout(args.timeout), headers={"User-Agent": "hackalem-wind-forecast"}) as client:
            summary = fetch(args.start, args.end, cache_dir, client, limiter, retries=args.retries, retry_unavailable=args.retry_unavailable)
    except FetchError as exc:
        logger.error("загрузка остановлена: %s", exc)
        return 1

    size_kb = sum(path.stat().st_size for path in cache_dir.iterdir() if path.is_file()) / 1024
    logger.info(
        "готово: в плане %d, было в кэше %d, скачано %d, недоступно %d. Кэш %s, %.0f КБ",
        summary.planned,
        summary.already_cached,
        summary.downloaded,
        summary.unavailable,
        cache_dir,
        size_kb,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
