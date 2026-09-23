"""Прогноз по датасету, который принес пользователь.

Обычный выпуск считает модель dev2 по нашей истории. Здесь истории нет: есть
CSV организаторов, который пользователь загрузил сам, и по нему надо ответить
на тот же вопрос — сколько станция выдаст в ближайшие 48 часов.

Путь короткий и целиком предметный:

1. Разобрать CSV. Колонки узнаются по русским заголовкам, лишние игнорируются.
2. Свернуть 10-минутные строки в часы усреднением. Час засчитывается, когда
   в нем есть хотя бы 4 точки из 6: по одной-двум точкам среднее за час врет.
3. Отбраковать мусор: пропуски, мощность вне ``[0, 1]``, отрицательный ветер,
   повторы по времени. Сколько и за что выброшено, уходит в ответ, а не
   в лог: пользователь должен видеть, что стало с его данными.
4. Построить **эмпирическую** кривую мощности: корзины ветра по 0,5 м/с,
   в каждой медиана нормированной мощности. Кривая делается неубывающей
   до номинала, ниже включения и выше отсечки она равна нулю.
5. Взять интервал **из тех же данных**: P10 и P90 это 10-й и 90-й процентили
   мощности, наблюденной в корзине. Не коридор вокруг медианы и не 0…1.
6. Прогнать через кривую ветер из погоды на момент выпуска и собрать ответ
   теми же схемами, что и обычный выпуск: фронтенд разбирает его тем же кодом.

Погода берется существующим путем — ``WeatherGateway`` из ``clients/weather.py``
с тем же ``as_of``, что и у обычного выпуска, поэтому погоды, опубликованной
позже момента выпуска, здесь тоже не бывает.

Состояния в памяти процесса нет: файл живет ровно один запрос, на диск
не пишется и никуда не сохраняется. Реплики бэкенда остаются взаимозаменяемыми.
"""

import io
import logging
import math
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import pandas as pd
from fastapi import UploadFile
from starlette.concurrency import run_in_threadpool

from src.core.exceptions import BusinessError
from src.modules.forecast import analyze, orchestrator, service
from src.modules.forecast.config import CUT_IN_MS, CUT_OUT_MS, LOCAL_OFFSET, TURBINES, rated_mw
from src.modules.forecast.decisions import LEVEL_WARN, DecisionLog
from src.modules.forecast.schemas import (
    ForecastHour,
    IssueSummary,
    PowerCurveBin,
    UploadDataset,
    UploadedFile,
    UploadForecastResponse,
    UploadWarning,
)

logger = logging.getLogger(__name__)

DATA_SOURCE_UPLOADED = "uploaded"

# --- коды ошибок ---------------------------------------------------------

CODE_NO_FILES = "UPLOAD_NO_FILES"
CODE_TOO_LARGE = "UPLOAD_TOO_LARGE"
CODE_BAD_FORMAT = "UPLOAD_BAD_FORMAT"
CODE_NOT_ENOUGH_DATA = "UPLOAD_NOT_ENOUGH_DATA"
CODE_BAD_VALUES = "UPLOAD_BAD_VALUES"

# --- пределы и пороги ----------------------------------------------------

#: Предел на файл. Оба контрольных файла организаторов около 6 МБ, то есть запас
#: четырехкратный. Это не параметр развертывания, а защита от заведомо чужого
#: файла, поэтому константа, а не переменная окружения.
MAX_FILE_BYTES = 25 * 1024 * 1024
#: Читаем кусками, чтобы превышение предела ловилось до того, как файл целиком
#: окажется в памяти процесса.
READ_CHUNK_BYTES = 1024 * 1024
#: Комплект это одна или две турбины, как в данных организаторов.
MAX_FILES = 2
#: Минимум для эмпирической кривой: месяц наблюдений. По двум неделям корзины
#: сильного ветра остаются пустыми, и кривая держится на интерполяции.
MIN_HOURS = 720
#: 10-минутный шаг: шесть точек в часе, час засчитывается при четырех из шести.
POINTS_PER_HOUR = 6
MIN_POINTS_PER_HOUR = 4
#: Корзины ветра эмпирической кривой.
BIN_WIDTH_MS = 0.5
BIN_COUNT = int(CUT_OUT_MS / BIN_WIDTH_MS)
#: Корзина с меньшим числом наблюдений не считается своей медианой: она берется
#: линейной интерполяцией между соседними, и это отмечается предупреждением.
MIN_BIN_SAMPLES = 20

# --- коды причин отбраковки ---------------------------------------------

DROP_BAD_TIMESTAMP = "bad_timestamp"
DROP_MISSING_VALUE = "missing_value"
DROP_NEGATIVE_WIND = "negative_wind"
DROP_POWER_OUT_OF_RANGE = "power_out_of_range"
DROP_DUPLICATE_TIME = "duplicate_time"
DROP_SPARSE_HOUR = "sparse_hour"

# --- коды предупреждений -------------------------------------------------

WARN_CURVE_INTERPOLATED = "CURVE_INTERPOLATED"
WARN_ROWS_DROPPED = "ROWS_DROPPED"
WARN_DEGRADED = "DEGRADED_WEATHER"

#: Заголовки данных организаторов. Ищем по подстроке, чтобы единицы измерения
#: в скобках и лишние пробелы не ломали разбор.
COLUMN_KEYS: dict[str, tuple[str, ...]] = {
    "time": ("статистическое время", "время"),
    "wind_ms": ("скорость ветра",),
    "power_norm": ("активная мощность", "мощность"),
    "temp_c": ("температура",),
}
REQUIRED_COLUMNS = ("time", "wind_ms", "power_norm")
COLUMN_TITLES = {
    "time": "Статистическое время",
    "wind_ms": "Средняя скорость ветра(m/s)",
    "power_norm": "Нормализованная активная мощность",
}


def bad_format(name: str, reason: str) -> BusinessError:
    return BusinessError(422, CODE_BAD_FORMAT, f"Файл {name} не разобран: {reason}", {"file": name})


@dataclass(frozen=True)
class ParsedFile:
    """Разобранный файл: часовые наблюдения и честный протокол отбраковки."""

    name: str
    turbine: str
    rows: int
    hourly: pd.DataFrame
    dropped: Counter
    step_minutes: int

    @property
    def dropped_rows(self) -> int:
        return int(sum(self.dropped.values()))


# --- разбор файла --------------------------------------------------------


def _read_frame(name: str, content: bytes) -> pd.DataFrame:
    """CSV в таблицу. Кодировка UTF-8 или Windows-1251, иначе это чужой файл."""
    if not content.strip():
        raise bad_format(name, "файл пустой")
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return pd.read_csv(io.BytesIO(content), encoding=encoding)
        except UnicodeDecodeError:
            continue
        except (pd.errors.ParserError, pd.errors.EmptyDataError) as error:
            raise bad_format(name, f"это не похоже на CSV ({error})") from error
    raise bad_format(name, "кодировка не распознана, ожидается UTF-8 или Windows-1251")


def _match_columns(columns) -> dict[str, str]:
    """Колонки по русским заголовкам. Первое совпадение выигрывает, лишние игнорируются."""
    found: dict[str, str] = {}
    for raw in columns:
        title = str(raw).replace("﻿", "").strip().lower()
        for field, keys in COLUMN_KEYS.items():
            if field not in found and any(key in title for key in keys):
                found[field] = raw
                break
    return found


def _clean(name: str, frame: pd.DataFrame, columns: dict[str, str]) -> tuple[pd.DataFrame, Counter]:
    """Разбор значений и отбраковка. Причина каждой выброшенной строки считается."""
    data = pd.DataFrame(
        {
            "time": pd.to_datetime(frame[columns["time"]], errors="coerce"),
            "wind_ms": pd.to_numeric(frame[columns["wind_ms"]], errors="coerce"),
            "power_norm": pd.to_numeric(frame[columns["power_norm"]], errors="coerce"),
            "temp_c": (
                pd.to_numeric(frame[columns["temp_c"]], errors="coerce") if "temp_c" in columns else pd.Series(float("nan"), index=frame.index)
            ),
        }
    )

    dropped: Counter = Counter()
    checks = (
        (DROP_BAD_TIMESTAMP, data["time"].isna()),
        (DROP_MISSING_VALUE, data["wind_ms"].isna() | data["power_norm"].isna()),
        (DROP_NEGATIVE_WIND, data["wind_ms"] < 0),
        (DROP_POWER_OUT_OF_RANGE, (data["power_norm"] < 0) | (data["power_norm"] > 1)),
    )
    for code, bad in checks:
        # Маски посчитаны по исходной таблице, а строки уже могли уйти
        # по более ранней причине: каждую строку считаем один раз.
        bad = bad.reindex(data.index, fill_value=False)
        count = int(bad.sum())
        if count:
            dropped[code] = count
            data = data[~bad]

    repeated = data["time"].duplicated(keep="first")
    if int(repeated.sum()):
        dropped[DROP_DUPLICATE_TIME] = int(repeated.sum())
        data = data[~repeated]

    if data.empty:
        raise BusinessError(
            422,
            CODE_BAD_VALUES,
            f"Файл {name}: ни одной годной строки, все отброшены проверкой",
            {"file": name, "drop_reasons": dict(dropped)},
        )
    return data.sort_values("time"), dropped


def _step_minutes(moments: pd.Series) -> int:
    """Шаг строк по данным, а не по обещанию: берется медиана разниц."""
    gaps = moments.diff().dropna()
    if gaps.empty:
        return 60
    minutes = int(round(gaps.median().total_seconds() / 60))
    return max(1, minutes)


def _to_hours(data: pd.DataFrame, step_minutes: int, dropped: Counter) -> pd.DataFrame:
    """10-минутные строки в часы усреднением, неполные часы выбрасываются.

    Правило «4 из 6» обобщается на любой шаг: час нужен заполненным на две трети.
    """
    expected = max(1, round(60 / step_minutes))
    required = max(1, math.ceil(expected * MIN_POINTS_PER_HOUR / POINTS_PER_HOUR))

    grouped = data.assign(hour=data["time"].dt.floor("h")).groupby("hour")
    counts = grouped.size()
    sparse = counts[counts < required]
    if not sparse.empty:
        dropped[DROP_SPARSE_HOUR] = int(sparse.sum())

    hourly = grouped[["wind_ms", "power_norm", "temp_c"]].mean()
    return hourly[counts >= required].reset_index()


def parse_file(name: str, content: bytes, turbine: str) -> ParsedFile:
    """Один CSV организаторов в часовые наблюдения. Синхронная, зовется из пула потоков."""
    frame = _read_frame(name, content)
    columns = _match_columns(frame.columns)
    missing = [field for field in REQUIRED_COLUMNS if field not in columns]
    if missing:
        raise bad_format(name, "не найдены колонки " + ", ".join(f"«{COLUMN_TITLES[field]}»" for field in missing))

    data, dropped = _clean(name, frame, columns)
    step = _step_minutes(data["time"])
    hourly = _to_hours(data, step, dropped)
    logger.info("Загруженный файл %s: %d строк, %d часов, отброшено %d", name, len(frame), len(hourly), sum(dropped.values()))
    return ParsedFile(name=name, turbine=turbine, rows=int(len(frame)), hourly=hourly, dropped=dropped, step_minutes=step)


# --- эмпирическая кривая мощности ---------------------------------------


def _clip(value: float) -> float:
    return round(min(1.0, max(0.0, float(value))), 4)


def _interpolated(known: list[int], value_of, number: int) -> float:
    """Значение в корзине без наблюдений: линейно между ближайшими заполненными."""
    if number <= known[0]:
        return value_of(known[0])
    if number >= known[-1]:
        return value_of(known[-1])
    right = bisect_right(known, number)
    low, high = known[right - 1], known[right]
    weight = (number - low) / (high - low)
    return value_of(low) + weight * (value_of(high) - value_of(low))


def build_power_curve(observations: pd.DataFrame) -> tuple[list[PowerCurveBin], list[UploadWarning]]:
    """Кривая мощности из наблюдений пользователя, с наблюденным же интервалом.

    В каждой корзине ветра шириной 0,5 м/с берутся медиана мощности и ее 10-й
    и 90-й процентили. Разреженные корзины достраиваются интерполяцией между
    соседними, кривая делается неубывающей, ниже включения и выше отсечки ноль.
    """
    inside = observations[(observations["wind_ms"] >= 0.0) & (observations["wind_ms"] < CUT_OUT_MS)]
    numbers = (inside["wind_ms"] // BIN_WIDTH_MS).astype(int).clip(0, BIN_COUNT - 1)

    raw: dict[int, tuple[float, float, float, int]] = {}
    for number, values in inside["power_norm"].groupby(numbers):
        raw[int(number)] = (float(values.median()), float(values.quantile(0.10)), float(values.quantile(0.90)), int(values.size))

    known = sorted(number for number, item in raw.items() if item[3] >= MIN_BIN_SAMPLES)
    if len(known) < 2:
        raise BusinessError(
            422,
            CODE_BAD_VALUES,
            f"По этим данным кривую не построить: наблюдения ложатся в {len(known)} корзин ветра при нужных двух и более",
            {"filled_bins": len(known), "bin_width_ms": BIN_WIDTH_MS},
        )

    curve: list[PowerCurveBin] = []
    interpolated: list[float] = []
    ceiling = 0.0
    for number in range(BIN_COUNT):
        center = (number + 0.5) * BIN_WIDTH_MS
        samples = raw.get(number, (0.0, 0.0, 0.0, 0))[3]
        if samples >= MIN_BIN_SAMPLES:
            power, low, high, _ = raw[number]
        else:
            power = _interpolated(known, lambda item: raw[item][0], number)
            low = _interpolated(known, lambda item: raw[item][1], number)
            high = _interpolated(known, lambda item: raw[item][2], number)
            interpolated.append(round(center, 2))

        if center < CUT_IN_MS:
            power = low = high = 0.0
        else:
            ceiling = max(ceiling, power)
            power = ceiling
            low = min(low, power)
            high = max(high, power)
        curve.append(PowerCurveBin(wind_ms=round(center, 2), power_norm=_clip(power), p10=_clip(low), p90=_clip(high), samples=samples))

    warnings: list[UploadWarning] = []
    if interpolated:
        warnings.append(
            UploadWarning(
                code=WARN_CURVE_INTERPOLATED,
                message=(
                    f"{len(interpolated)} корзин ветра из {BIN_COUNT} набрали меньше {MIN_BIN_SAMPLES} наблюдений "
                    f"и достроены интерполяцией между соседними: {_short_list(interpolated)} м/с"
                ),
            )
        )
    return curve, warnings


def _short_list(values: list[float], limit: int = 8) -> str:
    head = ", ".join(f"{value:g}" for value in values[:limit])
    return head if len(values) <= limit else f"{head} и еще {len(values) - limit}"


def curve_value(curve: list[PowerCurveBin], wind_ms: float) -> tuple[float, float, float]:
    """P10, P50, P90 для одной скорости ветра: линейно между центрами корзин."""
    if wind_ms < CUT_IN_MS or wind_ms >= CUT_OUT_MS:
        return 0.0, 0.0, 0.0

    centers = [point.wind_ms for point in curve]
    right = bisect_right(centers, wind_ms)
    if right == 0:
        low = high = curve[0]
        weight = 0.0
    elif right >= len(curve):
        low = high = curve[-1]
        weight = 0.0
    else:
        low, high = curve[right - 1], curve[right]
        weight = (wind_ms - low.wind_ms) / (high.wind_ms - low.wind_ms)

    def mix(left: float, right_value: float) -> float:
        return _clip(left + weight * (right_value - left))

    values = sorted((mix(low.p10, high.p10), mix(low.power_norm, high.power_norm), mix(low.p90, high.p90)))
    return values[0], values[1], values[2]


# --- сборка выпуска ------------------------------------------------------


def _turbine_names(requested: list[str] | None, count: int) -> list[str]:
    """Имена турбин по порядку файлов. Не назвали — берем имена турбин объекта."""
    defaults = [turbine.name for turbine in TURBINES]
    names: list[str] = []
    given = [name.strip() for name in (requested or [])]
    for index in range(count):
        name = given[index] if index < len(given) and given[index] else ""
        names.append(name[:32] if name else (defaults[index] if index < len(defaults) else f"T{index + 1}"))
    return names


async def _read_limited(file: UploadFile) -> bytes:
    """Содержимое файла с проверкой предела. Больше предела — читать дальше незачем."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(READ_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_FILE_BYTES:
            raise BusinessError(
                413,
                CODE_TOO_LARGE,
                f"Файл {file.filename or 'без имени'} больше предела {MAX_FILE_BYTES // (1024 * 1024)} МБ",
                {"file": file.filename, "limit_bytes": MAX_FILE_BYTES},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _parse_all(payloads: list[tuple[str, bytes]], names: list[str]) -> list[ParsedFile]:
    """Разбор всего комплекта. Тяжелая часть, поэтому уезжает в пул потоков."""
    return [parse_file(name, content, turbine) for (name, content), turbine in zip(payloads, names, strict=True)]


def _dataset(parsed: list[ParsedFile], hours: int) -> UploadDataset:
    reasons: Counter = Counter()
    for item in parsed:
        reasons.update(item.dropped)
    moments = pd.concat([item.hourly["hour"] for item in parsed])
    return UploadDataset(
        files=[UploadedFile(name=item.name, rows=item.rows, turbine=item.turbine) for item in parsed],
        period_start=moments.min().to_pydatetime(),
        period_end=moments.max().to_pydatetime(),
        hours=hours,
        step_minutes=min(item.step_minutes for item in parsed),
        dropped_rows=sum(item.dropped_rows for item in parsed),
        drop_reasons={code: count for code, count in sorted(reasons.items()) if count},
    )


def _journal_warnings(journal: DecisionLog) -> list[UploadWarning]:
    """Замечания шага ``fetch_weather``: переход на запасной источник не молчит."""
    return [UploadWarning(code=row.reason_code, message=row.reason) for row in journal.rows if row.level == LEVEL_WARN]


def _hours(curve: list[PowerCurveBin], collected, issue_time: datetime, names: list[str]) -> list[ForecastHour]:
    """Ветер из погоды через кривую пользователя, по каждой турбине комплекта."""
    meta = orchestrator.newest_rows(collected.rows)
    hours: list[ForecastHour] = []
    for name in names:
        capacity = rated_mw(name)
        for moment in sorted(collected.points):
            point = collected.points[moment]
            p10, p50, p90 = curve_value(curve, point.wind_ms)
            row = meta[moment]
            hours.append(
                ForecastHour(
                    valid_time_utc=moment,
                    valid_time_local=moment + LOCAL_OFFSET,
                    lead_h=int((moment - issue_time).total_seconds() // 3600),
                    turbine=name,
                    p10=p10,
                    p50=p50,
                    p90=p90,
                    p50_mw=round(p50 * capacity, 4),
                    wind_ms=point.wind_ms,
                    temp_c=point.temp_c,
                    actual=None,
                    source=collected.label,
                    run_init_utc=row.run_init_utc,
                    available_at_utc=row.available_at_utc,
                )
            )
    return hours


async def build_upload_forecast(
    files: list[UploadFile] | None,
    *,
    issue_date: date,
    turbine_names: list[str] | None = None,
) -> UploadForecastResponse:
    """Выпуск по загруженному комплекту: разбор, кривая, погода, сборка ответа."""
    files = [file for file in (files or []) if file is not None and (file.filename or file.size)]
    if not files:
        raise BusinessError(400, CODE_NO_FILES, "Не приложено ни одного файла: ожидается один или два CSV в поле files")
    if len(files) > MAX_FILES:
        raise BusinessError(
            422,
            CODE_BAD_FORMAT,
            f"Прислано {len(files)} файлов, а комплект это не больше {MAX_FILES}: по файлу на турбину",
            {"files": len(files), "limit": MAX_FILES},
        )
    service.ensure_known_issue(issue_date)
    issue_time = service.issue_time(issue_date)

    names = _turbine_names(turbine_names, len(files))
    payloads = [((file.filename or "без имени"), await _read_limited(file)) for file in files]
    parsed = await run_in_threadpool(_parse_all, payloads, names)

    observations = pd.concat([item.hourly for item in parsed], ignore_index=True)
    if len(observations) < MIN_HOURS:
        raise BusinessError(
            422,
            CODE_NOT_ENOUGH_DATA,
            f"Годных часов {len(observations)}, а нужно хотя бы {MIN_HOURS}: по двум неделям кривую мощности не построить",
            {"hours": int(len(observations)), "required": MIN_HOURS},
        )

    curve, warnings = await run_in_threadpool(build_power_curve, observations)

    journal = DecisionLog(issue_time_utc=issue_time)
    collected = await orchestrator.weather_for_issue(issue_time, journal)
    warnings.extend(_journal_warnings(journal))

    hours = _hours(curve, collected, issue_time, names)
    for hour, flags in zip(hours, analyze.risk_flags(hours, collected.points, collected.flags), strict=True):
        hour.flags = flags

    dataset = _dataset(parsed, int(len(observations)))
    if dataset.dropped_rows:
        warnings.insert(
            0,
            UploadWarning(
                code=WARN_ROWS_DROPPED,
                message=f"Отброшено {dataset.dropped_rows} строк: " + ", ".join(f"{code} — {count}" for code, count in dataset.drop_reasons.items()),
            ),
        )
    degraded = analyze.FLAG_DEGRADED in collected.flags
    if degraded:
        warnings.append(UploadWarning(code=WARN_DEGRADED, message="Погода на момент выпуска собрана не в полной конфигурации источников"))

    target_date = issue_date + timedelta(days=1)
    width = sum(hour.p90 - hour.p10 for hour in hours) / len(hours)
    kpi = service.build_kpi(hours, target_date, width / 2 * 100)
    counts = analyze.flag_counts(hours)
    risks = ", ".join(f"{flag}: {count} ч" for flag, count in counts.items())

    return UploadForecastResponse(
        issue=IssueSummary(
            issue_date=issue_date,
            issue_time_utc=issue_time,
            target_date=target_date,
            version=1,
            status="ready",
            degraded=degraded,
            source=collected.label,
            flagged_hours=sum(1 for hour in hours if hour.flags),
        ),
        kpi=kpi,
        summary=(
            f"Прогноз посчитан по загруженному комплекту: {len(parsed)} файл(ов), {dataset.hours} часов наблюдений "
            f"с {dataset.period_start.date().isoformat()} по {dataset.period_end.date().isoformat()}. "
            f"Кривая мощности и интервал P10…P90 взяты из этих же данных, ветер — из погоды на момент выпуска "
            f"({collected.label}). На сутки {target_date.isoformat()} ожидается средняя загрузка "
            f"{round(kpi.mean_load_pct)}%, выработка {kpi.day_energy_mwh} МВт·ч. "
            + (f"Отмечены риски: {risks}." if risks else "Рисков по часам не отмечено.")
        ),
        hours=hours,
        dataset=dataset,
        power_curve=curve,
        warnings=warnings,
        data_source=DATA_SOURCE_UPLOADED,
    )
