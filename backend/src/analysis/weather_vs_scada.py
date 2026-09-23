"""Сравнение погоды из Open-Meteo с данными SCADA турбин.

Диагностика, а не часть пайплайна прогноза. Отвечает на вопросы, которых нет в ТЗ:
в каком часовом поясе записано время SCADA, с какой высотой ветра модели сходятся
лучше и насколько точны прогнозы четырех моделей погоды на +24 и +48 ч по сравнению
с их средним (ансамблем).

ERA5 здесь используется только для диагностики. В прогноз он не попадает, поэтому
модуль лежит вне ``src/forecast`` и не нарушает правило из GOAL.md.

Запуск из папки backend:

    uv run python -m src.analysis.weather_vs_scada            # из кэша, сеть не нужна
    uv run python -m src.analysis.weather_vs_scada --refresh  # перекачать погоду

Кэш ответов API: ``data/weather_compare/``. Отчет: ``reports/weather_vs_scada.md``.
"""

import argparse
import time
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[3]
SCADA_FILES = {
    "T1": REPO / "data" / "Dataset HackAlemAI turbine 1.csv",
    "T2": REPO / "data" / "Dataset HackAlemAI turbine 2.csv",
}
CACHE_DIR = REPO / "data" / "weather_compare"
REPORT_PATH = REPO / "reports" / "weather_vs_scada.md"

# Середина между турбинами, они стоят в ~330 м друг от друга, это одна ячейка сетки.
LAT, LON = 43.6442, 78.5372

ERA5_URL = "https://archive-api.open-meteo.com/v1/archive"
PREV_RUNS_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"

# Модель -> высоты ветра, которые она реально отдает, и шаг прогонов в часах.
# Проверено запросами к API: у ecmwf_ifs025 есть только 100 м, у gem_global нет 100 м.
MODELS: dict[str, tuple[tuple[int, ...], int]] = {
    "ecmwf_ifs025": ((100,), 6),
    "gfs_global": ((80, 100, 120), 6),
    "icon_global": ((80, 100, 120), 6),
    "gem_global": ((80, 120), 12),
}
# Высота, по которой модель сравнивается с SCADA: ступица 80 м, если модель ее отдает.
SKILL_HEIGHT = {"ecmwf_ifs025": 100, "gfs_global": 80, "icon_global": 80, "gem_global": 80}
MODEL_LABEL = {"ecmwf_ifs025": "ECMWF IFS 0.25°", "gfs_global": "GFS", "icon_global": "ICON", "gem_global": "GEM"}

SCADA_START = date(2023, 3, 11)
SCADA_END = date(2026, 1, 31)
NWP_START = date(2024, 1, 1)
# Смена пояса Алматинской области: с 01.03.2024 UTC+5 вместо UTC+6.
TZ_SWITCH = pd.Timestamp("2024-03-01")
# Разбиение для честной оценки поправки: учим на первой части, меряем на второй.
SPLIT = pd.Timestamp("2025-08-01")
LAGS = range(-3, 10)

# Колонки таблиц отчета: (ключ строки, заголовок).
HEIGHT_COLUMNS = [
    ("src", "Источник"),
    ("h", "Высота"),
    ("n", "Часов"),
    ("corr", "corr"),
    ("bias", "bias, м/с"),
    ("mae", "MAE, м/с"),
    ("ratio", "ratio"),
]
SKILL_COLUMNS = [
    ("lead", "Заблаговременность"),
    ("model", "Модель"),
    ("n", "Часов"),
    ("raw_mae", "MAE"),
    ("raw_rmse", "RMSE"),
    ("raw_bias", "bias"),
    ("corr", "corr"),
    ("mos_mae", "MAE после MOS"),
    ("mos_rmse", "RMSE после MOS"),
]
AVAILABILITY_COLUMNS = [
    ("model", "Модель"),
    ("col", "Колонка"),
    ("first", "Данные с"),
    ("missing", "Пропусков, %"),
]
COVERAGE_COLUMNS = [
    ("t", "Турбина"),
    ("first", "С"),
    ("last", "По"),
    ("hours", "Часов"),
    ("wind", "Средний ветер, м/с"),
    ("temp", "Средняя t, °C"),
]


# ---------------------------------------------------------------- SCADA


def load_scada_hourly(path: Path) -> pd.DataFrame:
    """10-минутная SCADA -> часовые средние, центрированные на час.

    Прогноз погоды дает мгновенное значение в hh:00, поэтому в час hh попадают
    записи из [hh-30 мин, hh+30 мин). Час без хотя бы 4 записей из 6 отбрасывается.
    Время остается локальным и без пояса: пояс как раз и ищется.
    """
    raw = pd.read_csv(path)
    raw.columns = ["id", "time", "wind", "power", "temp"]
    raw["time"] = pd.to_datetime(raw["time"], format="%Y-%m-%d %H:%M:%S")
    raw = raw.drop_duplicates("time")
    raw["hour"] = (raw["time"] + pd.Timedelta(minutes=30)).dt.floor("h")
    grouped = raw.groupby("hour")
    hourly = grouped[["wind", "power", "temp"]].mean()
    hourly = hourly[grouped.size() >= 4]
    hourly.index.name = "time_local"
    return hourly


def load_site() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Средний по двум турбинам ветер и температура, плюс каждая турбина отдельно."""
    turbines = {name: load_scada_hourly(path) for name, path in SCADA_FILES.items()}
    site = pd.concat({name: df[["wind", "temp"]] for name, df in turbines.items()}, axis=1, sort=True)
    both = pd.DataFrame(
        {
            "wind": site.xs("wind", axis=1, level=1).mean(axis=1),
            "temp": site.xs("temp", axis=1, level=1).mean(axis=1),
        }
    )
    return both.dropna(), turbines


# ---------------------------------------------------------------- Open-Meteo


def _get(url: str, params: dict, client: httpx.Client) -> dict:
    """GET с повторами. Ответ с ошибкой API превращается в понятное исключение."""
    for attempt in range(4):
        try:
            response = client.get(url, params=params)
        except httpx.HTTPError as exc:
            if attempt == 3:
                raise RuntimeError(f"Open-Meteo недоступен: {exc}") from exc
        else:
            if response.status_code == 200:
                return response.json()
            if response.status_code != 429 and response.status_code < 500:
                raise RuntimeError(f"Open-Meteo {response.status_code}: {response.text[:300]}")
        time.sleep(2**attempt)
    raise RuntimeError(f"Open-Meteo не ответил после повторов: {url}")


def _hourly_frame(payload: dict) -> pd.DataFrame:
    frame = pd.DataFrame(payload["hourly"])
    frame["time"] = pd.to_datetime(frame["time"])
    return frame.set_index("time")


def _year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    chunks = []
    for year in range(start.year, end.year + 1):
        lo = max(start, date(year, 1, 1))
        hi = min(end, date(year, 12, 31))
        chunks.append((lo, hi))
    return chunks


def _cached(name: str, refresh: bool, fetch) -> pd.DataFrame:
    """Ответ API из кэша, при ``refresh`` или пустом кэше сначала перекачивается.

    Кэш хранит значения с точностью 0.01, и отчет всегда строится по прочитанному
    кэшу. Иначе запуск с ``--refresh`` и следующий запуск из кэша дали бы разные цифры.
    """
    path = CACHE_DIR / f"{name}.csv.gz"
    if refresh or not path.exists():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fetch().sort_index().to_csv(path, compression={"method": "gzip", "mtime": 0}, float_format="%.2f")
    return pd.read_csv(path, parse_dates=["time"], index_col="time")


def fetch_era5(refresh: bool, client: httpx.Client) -> pd.DataFrame:
    """ERA5: фактическая погода, только для диагностики пояса и высоты."""

    def fetch() -> pd.DataFrame:
        parts = []
        for lo, hi in _year_chunks(SCADA_START, SCADA_END):
            params = {
                "latitude": LAT,
                "longitude": LON,
                "start_date": lo.isoformat(),
                "end_date": hi.isoformat(),
                "hourly": "wind_speed_10m,wind_speed_100m,temperature_2m",
                "models": "era5",
                "wind_speed_unit": "ms",
                "timezone": "GMT",
            }
            parts.append(_hourly_frame(_get(ERA5_URL, params, client)))
        return pd.concat(parts)

    return _cached("era5", refresh, fetch)


def prev_runs_columns(heights: tuple[int, ...]) -> list[str]:
    cols = ["temperature_2m"]
    for h in heights:
        cols += [f"wind_speed_{h}m", f"wind_speed_{h}m_previous_day1", f"wind_speed_{h}m_previous_day2"]
    return cols


def fetch_prev_runs(model: str, refresh: bool, client: httpx.Client) -> pd.DataFrame:
    """Previous Runs API: что модель предсказывала за 24 и 48 ч до каждого часа."""
    heights, _ = MODELS[model]

    def fetch() -> pd.DataFrame:
        parts = []
        for lo, hi in _year_chunks(NWP_START, SCADA_END):
            params = {
                "latitude": LAT,
                "longitude": LON,
                "start_date": lo.isoformat(),
                "end_date": hi.isoformat(),
                "hourly": ",".join(prev_runs_columns(heights)),
                "models": model,
                "wind_speed_unit": "ms",
                "timezone": "GMT",
            }
            parts.append(_hourly_frame(_get(PREV_RUNS_URL, params, client)))
        return pd.concat(parts)

    return _cached(f"prev_runs_{model}", refresh, fetch)


def prev_day_run_init(valid_time: pd.Timestamp, n: int, cycle_h: int) -> pd.Timestamp:
    """Какой прогон стоит за ``previous_dayN`` в час ``valid_time``.

    Для GFS, ICON и ECMWF проверено сверкой с Single Runs API за август 2026:
    init = floor_6h(t) - 24·N ч, то есть заблаговременность 24·N + (t mod 6) ч.
    Для GEM (прогоны раз в 12 ч) точных прогонов в API нет, правило floor_12h
    взято консервативно: оно не может дать прогон новее настоящего.
    """
    return valid_time.floor(f"{cycle_h}h") - pd.Timedelta(hours=24 * n)


def lead_label(n: int, cycle_h: int) -> str:
    """Диапазон заблаговременности ``previous_dayN`` по правилу ``prev_day_run_init``."""
    return f"+{24 * n}…{24 * n + cycle_h - 1} ч"


# ---------------------------------------------------------------- метрики


def lag_correlations(local: pd.Series, utc: pd.Series, lags=LAGS) -> pd.Series:
    """Корреляция SCADA с эталоном при гипотезе «UTC = локальное время − k ч»."""
    out = {}
    for k in lags:
        shifted = local.copy()
        shifted.index = shifted.index - pd.Timedelta(hours=k)
        joined = pd.concat([shifted, utc], axis=1, join="inner", sort=True).dropna()
        out[k] = joined.iloc[:, 0].corr(joined.iloc[:, 1]) if len(joined) > 100 else np.nan
    return pd.Series(out, name="corr")


def to_utc(local: pd.DataFrame, offset_before: int, offset_after: int) -> pd.DataFrame:
    """Локальное время SCADA -> UTC с учетом смены пояса 01.03.2024."""
    shift = np.where(local.index < TZ_SWITCH, offset_before, offset_after)
    out = local.copy()
    out.index = local.index - pd.to_timedelta(shift, unit="h")
    out.index.name = "time_utc"
    return out[~out.index.duplicated()]


def error_stats(obs: pd.Series, pred: pd.Series) -> dict[str, float]:
    joined = pd.concat([obs.rename("obs"), pred.rename("pred")], axis=1, sort=True).dropna()
    err = joined["pred"] - joined["obs"]
    return {
        "n": len(joined),
        "mae": err.abs().mean(),
        "bias": err.mean(),
        "rmse": float(np.sqrt((err**2).mean())),
        "corr": joined["obs"].corr(joined["pred"]),
    }


def linear_fit(obs: pd.Series, pred: pd.Series) -> tuple[float, float]:
    """Коэффициенты поправки obs ≈ a + b·pred по методу наименьших квадратов."""
    joined = pd.concat([obs.rename("obs"), pred.rename("pred")], axis=1, sort=True).dropna()
    b, a = np.polyfit(joined["pred"], joined["obs"], 1)
    return float(a), float(b)


def model_levels(nwp: dict[str, pd.DataFrame]) -> dict[str, float]:
    """Средний ветер каждой модели относительно среднего четырех, в процентах.

    Свежий прогон на высоте сравнения, только часы, где есть все модели.
    """
    winds = pd.concat({m: frame[f"wind_speed_{SKILL_HEIGHT[m]}m"] for m, frame in nwp.items()}, axis=1, sort=True).dropna()
    means = winds.mean()
    return {m: float((means[m] / means.mean() - 1) * 100) for m in nwp}


def quarters_with_temp_lag(quarter_lags: list[tuple[int, int]]) -> int:
    """Сколько кварталов, где лучший сдвиг температуры ровно на час больше, чем у ветра.

    ``quarter_lags``: пары (сдвиг ветра, сдвиг температуры) по кварталам.
    """
    return sum(temp == wind + 1 for wind, temp in quarter_lags)


def md_table(rows: list[dict], columns: list[tuple[str, str]], floatfmt: str = ".2f") -> str:
    """Markdown-таблица без tabulate. columns: [(ключ, заголовок)]."""

    def cell(value) -> str:
        if isinstance(value, float | np.floating):
            return "—" if np.isnan(value) else format(value, floatfmt)
        return str(value)

    head = "| " + " | ".join(title for _, title in columns) + " |"
    sep = "|" + "|".join("---" for _ in columns) + "|"
    body = ["| " + " | ".join(cell(row.get(key, "")) for key, _ in columns) + " |" for row in rows]
    return "\n".join([head, sep, *body])


# ---------------------------------------------------------------- отчет


def best_lag(corrs: pd.Series) -> int:
    return int(corrs.idxmax())


def fresh_ensemble(nwp: dict[str, pd.DataFrame]) -> pd.Series:
    """Средний ветер свежих прогонов четырех моделей на высоте сравнения."""
    return pd.concat([frame[f"wind_speed_{SKILL_HEIGHT[m]}m"] for m, frame in nwp.items()], axis=1, sort=True).mean(axis=1)


def tz_section(site: pd.DataFrame, era5: pd.DataFrame, nwp: dict[str, pd.DataFrame]) -> tuple[str, int, int]:
    """Пояс времени SCADA решается по ветру.

    Температура для этого не годится: датчик на турбине запаздывает относительно
    воздуха, и ее лучший сдвиг в большинстве кварталов на час больше, чем у ветра.
    """
    ensemble = fresh_ensemble(nwp)
    periods = {
        "до 01.03.2024": site[site.index < TZ_SWITCH],
        "с 01.03.2024": site[site.index >= TZ_SWITCH],
    }
    rows, best = [], {}
    for name, part in periods.items():
        wind = lag_correlations(part["wind"], era5["wind_speed_100m"])
        temp = lag_correlations(part["temp"], era5["temperature_2m"])
        best[name] = best_lag(wind)
        for k in range(4, 9):
            rows.append({"period": name, "k": f"UTC+{k}", "wind": wind[k], "temp": temp[k]})

    quarter_rows, quarter_lags = [], []
    for (year, quarter), part in site.groupby([site.index.year, site.index.quarter]):
        if len(part) < 500:
            continue
        wind = lag_correlations(part["wind"], era5["wind_speed_100m"])
        ens = lag_correlations(part["wind"], ensemble)
        temp = lag_correlations(part["temp"], era5["temperature_2m"])
        quarter_lags.append((best_lag(wind), best_lag(temp)))
        quarter_rows.append(
            {
                "q": f"{year} Q{quarter}",
                "wk": f"UTC+{best_lag(wind)}",
                "ek": f"UTC+{best_lag(ens)}" if ens.notna().any() else "—",
                "tk": f"UTC+{best_lag(temp)}",
            }
        )
    temp_later = quarters_with_temp_lag(quarter_lags)

    before, after = best["до 01.03.2024"], best["с 01.03.2024"]
    switch_note = (
        "Сдвига на границе 01.03.2024 нет: часы SCADA на UTC+5 не переводились."
        if before == after
        else f"На границе 01.03.2024 пояс меняется с UTC+{before} на UTC+{after}."
    )
    text = f"""## 1. Часовой пояс времени SCADA

В ТЗ пояс не указан. Проверка: сдвигаем время SCADA на k часов и смотрим, при каком
сдвиге ветер лучше всего совпадает с ERA5 и со средним свежих прогонов четырех моделей
(время у них — UTC).

{md_table(rows, [("period", "Период"), ("k", "Гипотеза: время SCADA ="), ("wind", "corr ветра с ERA5"), ("temp", "corr температуры с ERA5")], ".3f")}

Лучший сдвиг по кварталам:

{md_table(quarter_rows, [("q", "Квартал"), ("wk", "ветер, ERA5"), ("ek", "ветер, среднее 4 моделей"), ("tk", "температура, ERA5")])}

Пояс определяется по ветру. Температура в {temp_later} из {len(quarter_lags)} кварталов
показывает сдвиг ровно на час больше, чем ветер. Вероятная причина: датчик на турбине
прогревается и остывает с запаздыванием, поэтому ее суточный ход отстает от воздуха.

**Вывод:** время SCADA — UTC+{before} до 01.03.2024 и UTC+{after} после. {switch_note}
Перевод в UTC: `time_utc = time_scada − {after} ч`. Ниже везде используется это правило.
"""
    return text, before, after


def height_section(site_utc: pd.DataFrame, era5: pd.DataFrame, nwp: dict[str, pd.DataFrame]) -> str:
    rows = []
    for h, col in ((10, "wind_speed_10m"), (100, "wind_speed_100m")):
        s = error_stats(site_utc["wind"], era5[col])
        rows.append({"src": "ERA5", "h": f"{h} м", **s, "ratio": site_utc["wind"].mean() / era5[col].reindex(site_utc.index).mean()})
    for model, frame in nwp.items():
        for h in MODELS[model][0]:
            col = f"wind_speed_{h}m"
            s = error_stats(site_utc["wind"], frame[col])
            joined = pd.concat([site_utc["wind"], frame[col]], axis=1, join="inner", sort=True).dropna()
            rows.append(
                {"src": MODEL_LABEL[model] + " (свежий прогон)", "h": f"{h} м", **s, "ratio": joined.iloc[:, 0].mean() / joined.iloc[:, 1].mean()}
            )
    levels = ", ".join(f"{MODEL_LABEL[m]} {pct:+.0f}%" for m, pct in model_levels(nwp).items())
    return f"""## 2. Высота замера ветра

В ТЗ высота не указана, в плане принято 80 м (ступица GW109). Сравниваем средний ветер
двух турбин с ветром на разных высотах. `ratio` — средний ветер SCADA к среднему ветру
модели: 1.0 значит, что уровни совпадают.

{md_table(rows, HEIGHT_COLUMNS)}

Корреляция почти не зависит от высоты: соседние уровни одной модели связаны почти
линейно. Уровень лучше показывают `bias` и `ratio`.

Уровни моделей между собой: средний ветер свежего прогона на высоте сравнения
относительно среднего четырех моделей, по общим часам: {levels}.
Насколько смещение моделей снимает поправка (MOS), видно в разделе 3.
"""


def skill_section(site_utc: pd.DataFrame, nwp: dict[str, pd.DataFrame]) -> tuple[str, dict[int, dict]]:
    """Ошибка прогноза ветра каждой модели и их среднего на +24 и +48 ч.

    Возвращает текст раздела и сводку для выводов по каждой заблаговременности:
    лучшая одиночная модель и ансамбль, MAE в м/с как есть и после поправки.

    Поправка ансамбля обучается на самом среднем четырех моделей. Среднее четырех
    уже поправленных прогонов не годится: каждая поправка по МНК сжимает прогноз
    к среднему, и среднее сжатых прогонов сжато сильнее нужного.
    """
    obs = site_utc["wind"]
    common = obs.index
    for model, frame in nwp.items():
        h = SKILL_HEIGHT[model]
        for n in (1, 2):
            common = common.intersection(frame[f"wind_speed_{h}m_previous_day{n}"].dropna().index)
    obs = obs.reindex(common)
    train, test = obs[obs.index < SPLIT], obs[obs.index >= SPLIT]
    ensemble_cycle = max(MODELS[m][1] for m in nwp)

    rows, spread_rows, summary = [], [], {}
    biases, mos_helps, spread_grows, spread_level_corr = {}, [], {}, {}
    for n in (1, 2):
        raw_preds, mos_preds = {}, {}
        for model, frame in nwp.items():
            pred = frame[f"wind_speed_{SKILL_HEIGHT[model]}m_previous_day{n}"].reindex(common)
            a, b = linear_fit(train, pred[train.index])
            raw_preds[model] = pred
            mos_preds[model] = a + b * pred
        raw_preds["ensemble"] = pd.concat(raw_preds.values(), axis=1).mean(axis=1)
        a, b = linear_fit(train, raw_preds["ensemble"][train.index])
        mos_preds["ensemble"] = a + b * raw_preds["ensemble"]
        spread = pd.concat([mos_preds[m] for m in nwp], axis=1).std(axis=1)

        raw_mae, mos_mae = {}, {}
        for model in [*nwp, "ensemble"]:
            raw = error_stats(test, raw_preds[model][test.index])
            mos = error_stats(test, mos_preds[model][test.index])
            raw_mae[model], mos_mae[model] = raw["mae"], mos["mae"]
            if model != "ensemble":
                biases.setdefault(model, raw["bias"])
                mos_helps.append(mos["mae"] < raw["mae"])
            label = "**Ансамбль (среднее 4)**" if model == "ensemble" else f"{MODEL_LABEL[model]} ({SKILL_HEIGHT[model]} м)"
            cycle_h = ensemble_cycle if model == "ensemble" else MODELS[model][1]
            rows.append(
                {
                    "lead": lead_label(n, cycle_h),
                    "model": label,
                    "n": raw["n"],
                    "raw_mae": raw["mae"],
                    "raw_rmse": raw["rmse"],
                    "raw_bias": raw["bias"],
                    "mos_mae": mos["mae"],
                    "mos_rmse": mos["rmse"],
                    "corr": raw["corr"],
                }
            )
        best_single = min(nwp, key=raw_mae.get)
        best_mos = min(nwp, key=mos_mae.get)
        summary[n] = {
            "best": MODEL_LABEL[best_single],
            "best_mae": raw_mae[best_single],
            "ens_mae": raw_mae["ensemble"],
            "best_mos": MODEL_LABEL[best_mos],
            "best_mos_mae": mos_mae[best_mos],
            "ens_mos_mae": mos_mae["ensemble"],
        }

        # Разброс моделей как мера неуверенности: при большом разбросе ошибка выше?
        err = (raw_preds["ensemble"][test.index] - test).abs()
        buckets = pd.qcut(spread[test.index], 3, labels=["малый", "средний", "большой"])
        bucket_mae = [err[buckets == k].mean() for k in buckets.cat.categories]
        for k, mae in zip(buckets.cat.categories, bucket_mae, strict=True):
            spread_rows.append({"lead": f"+{24 * n} ч", "b": str(k), "sp": spread[test.index][buckets == k].mean(), "mae": mae})
        spread_grows[n] = bool(np.all(np.diff(bucket_mae) > 0))
        spread_level_corr[n] = spread[test.index].corr(raw_preds["ensemble"][test.index])
        summary[n]["spread_grows"] = spread_grows[n]

    over = [MODEL_LABEL[m] for m, b in biases.items() if b > 0]
    under = [MODEL_LABEL[m] for m, b in biases.items() if b < 0]
    bias_note = (
        f"Смещения моделей на +24 ч разного знака: {' и '.join(over)} завышают, {' и '.join(under)} занижают, поэтому в среднем они частично гасятся."
        if over and under
        else "Смещения всех моделей на +24 ч одного знака, в среднем они не гасятся."
    )
    mos_note = (
        "Поправка снижает MAE каждой модели на обеих заблаговременностях."
        if all(mos_helps)
        else f"Поправка снижает MAE одиночной модели в {sum(mos_helps)} случаях из {len(mos_helps)}."
    )
    compare = "; ".join(f"на +{24 * n} ч {s['ens_mos_mae']:.2f} против {s['best_mos_mae']:.2f} у {s['best_mos']}" for n, s in summary.items())
    if all(spread_grows.values()):
        grows = "С ростом разброса ошибка ансамбля растет на обеих заблаговременностях."
    else:
        grows = (
            "С ростом разброса ошибка ансамбля растет не монотонно на " + " и ".join(f"+{24 * n} ч" for n, g in spread_grows.items() if not g) + "."
        )
    period = f"{common.min():%d.%m.%Y}–{common.max():%d.%m.%Y}"
    text = f"""## 3. Точность прогнозов четырех моделей и ансамбля

Прогнозы из Previous Runs API: `previous_day1` — прогон, выпущенный за 24–29 ч до часа,
`previous_day2` — за 48–53 ч. У GEM прогоны раз в 12 ч, поэтому по правилу из раздела 4
его заблаговременность до 35 и 59 ч. Сравнение идет с ветром SCADA, переведенным в UTC
по правилу из раздела 1.

Общий период, где есть все модели: {period}, часов: {len(common)}.
Поправка (MOS: `ветер = a + b·прогноз`) обучена на данных до {SPLIT:%d.%m.%Y}, все метрики
ниже посчитаны на отложенном периоде после этой даты. У одиночной модели поправка
обучается на ее прогнозе, у ансамбля — на среднем четырех сырых прогнозов.

{md_table(rows, SKILL_COLUMNS)}

Все ошибки в м/с. Колонки без пометки — прогноз как есть. {bias_note}
{mos_note} Честное сравнение ансамбля с одной моделью — после поправки у обоих: {compare}.

Разброс четырех моделей и ошибка ансамбля (часы разбиты на три равные группы по разбросу):

{md_table(spread_rows, [("lead", "Заблаговременность"), ("b", "Разброс"), ("sp", "средний разброс, м/с"), ("mae", "MAE ансамбля, м/с")])}

{grows} Разброс связан с силой ветра (корреляция с прогнозом ансамбля {spread_level_corr[1]:.2f} на +24 ч
и {spread_level_corr[2]:.2f} на +48 ч), поэтому это кандидат в признаки неуверенности
для P10–P90, а пользу от него показывает бэктест квантильной модели.
"""
    return text, summary


def availability_section(nwp: dict[str, pd.DataFrame]) -> tuple[str, dict]:
    """Текст раздела и сводка для выводов: самое раннее и самое позднее начало архива, максимум пропусков."""
    rows, firsts, missing = [], [], []
    for model, frame in nwp.items():
        h = SKILL_HEIGHT[model]
        for n in (1, 2):
            s = frame[f"wind_speed_{h}m_previous_day{n}"]
            first = s.first_valid_index()
            window = s[s.index >= first] if first is not None else s
            firsts.append(first)
            missing.append(window.isna().mean() * 100)
            rows.append(
                {
                    "model": MODEL_LABEL[model],
                    "col": f"previous_day{n}",
                    "first": f"{first:%d.%m.%Y}" if first is not None else "нет",
                    "missing": missing[-1],
                }
            )
    found = [f for f in firsts if f is not None]
    summary = {
        "all_present": len(found) == len(firsts),
        "first_min": min(found) if found else None,
        "first_max": max(found) if found else None,
        "missing_max": max(missing),
    }
    text = f"""## 4. Доступность архива и правило прогонов

{md_table(rows, AVAILABILITY_COLUMNS, ".1f")}

**Какой прогон стоит за `previous_dayN`.** Проверено сверкой значений с Single Runs API
(точные прогоны) за 31.07–05.08.2026 для GFS, ICON, ECMWF IFS 0.25° и ECMWF IFS 9 км.
Сверка делалась вручную, этот скрипт ее не повторяет; правило в пайплайне —
`src/forecast/weather/prev_runs_rule.py`. Результат сверки:
значение `previous_dayN` в час t совпадает с прогоном init = floor_6h(t) − 24·N ч,
то есть заблаговременность равна 24·N + (t mod 6) ч. У ECMWF IFS 0.25° и GEM данные
идут с шагом 3 ч, и для них это верно только в часы, кратные 3. Остальные часы
Open-Meteo интерполирует по точкам floor_3h(t) − 3 ч … floor_3h(t) + 6 ч, склеенным
из разных прогонов, и последняя точка бывает из следующего прогона (сверка с Single
Runs за апрель–июнь 2026). Для таких часов прогоном считается самый новый из окна
интерполяции, см. `weather/prev_runs_rule.py`. Для GEM точных прогонов в Single
Runs нет (`modelRunUnavailable`), прогоны у него раз в 12 ч, поэтому для него
init = floor_12h(t) − 24·N ч с той же поправкой на 3-часовые данные.

Для выпуска в момент T подходит `previous_dayN` с минимальным N, для которого
init + задержка публикации ≤ T. Брать `previous_day1` вслепую нельзя: для часов
+25…+48 от T это прогон, опубликованный после T.
"""
    return text, summary


def build_report(refresh: bool) -> str:
    with httpx.Client(timeout=httpx.Timeout(120.0, connect=15.0)) as client:
        era5 = fetch_era5(refresh, client)
        nwp = {model: fetch_prev_runs(model, refresh, client) for model in MODELS}

    site, turbines = load_site()
    tz_text, before, after = tz_section(site, era5, nwp)
    site_utc = to_utc(site, before, after)
    skill_text, skill = skill_section(site_utc, nwp)
    availability_text, archive = availability_section(nwp)
    levels = model_levels(nwp)

    gain = {n: (1 - s["ens_mae"] / s["best_mae"]) * 100 for n, s in skill.items()}
    gain_mos = {n: (1 - s["ens_mos_mae"] / s["best_mos_mae"]) * 100 for n, s in skill.items()}
    ensemble_wins = all(g > 0 for g in [*gain.values(), *gain_mos.values()])
    ensemble_head = "Ансамбль четырех моделей точнее любой одной" if ensemble_wins else "Ансамбль четырех моделей не везде точнее лучшей модели"
    spread_line = (
        "Ошибка ансамбля растет вместе с разбросом моделей, это кандидат в признаки неуверенности."
        if all(s["spread_grows"] for s in skill.values())
        else "Ошибка ансамбля растет с разбросом моделей не на всех заблаговременностях."
    )
    archive_gaps = "без пропусков" if archive["missing_max"] == 0 else f"пропусков до {archive['missing_max']:.1f}%"
    archive_line = (
        f"**Архив Previous Runs есть у всех четырех моделей, {archive_gaps}.** Начинается "
        f"с {archive['first_min']:%d.%m.%Y}, у последней колонки с {archive['first_max']:%d.%m.%Y}."
        if archive["all_present"]
        else "**Архив Previous Runs есть не у всех моделей.**"
    )
    tz_line = "весь период, перехода на UTC+5 в марте 2024 в данных нет" if before == after else f"после 01.03.2024, до этой даты UTC+{before}"
    summary = f"""## Главные выводы

1. **Время SCADA записано в UTC+{after}** — {tz_line}. Перевод:
   `time_utc = time_scada − {after} ч`. Раздел 1.
2. **Высоту замера по данным не определить.** Уровни ветра моделей относительно их общего
   среднего от {min(levels.values()):+.0f}% до {max(levels.values()):+.0f}%. Раздел 2.
3. **{ensemble_head}.** MAE ветра, м/с, ансамбль против лучшей модели:
   - после поправки у обоих: +24 ч {skill[1]["ens_mos_mae"]:.2f} против {skill[1]["best_mos_mae"]:.2f} ({skill[1]["best_mos"]}),
     на {gain_mos[1]:.0f}% меньше; +48 ч {skill[2]["ens_mos_mae"]:.2f} против {skill[2]["best_mos_mae"]:.2f} ({skill[2]["best_mos"]}),
     на {gain_mos[2]:.0f}% меньше;
   - без поправки: +24 ч {skill[1]["ens_mae"]:.2f} против {skill[1]["best_mae"]:.2f} ({skill[1]["best"]}), на {gain[1]:.0f}% меньше;
     +48 ч {skill[2]["ens_mae"]:.2f} против {skill[2]["best_mae"]:.2f} ({skill[2]["best"]}), на {gain[2]:.0f}% меньше.
     Поправка улучшает одиночные модели сильнее, чем ансамбль, поэтому для решений берем цифры после поправки.

   {spread_line} Раздел 3.
4. {archive_line}
   Какой прогон стоит за `previous_dayN`, проверено вручную по точным прогонам. Раздел 4.
"""

    coverage = [
        {
            "t": name,
            "first": f"{df.index.min():%d.%m.%Y}",
            "last": f"{df.index.max():%d.%m.%Y}",
            "hours": len(df),
            "wind": df["wind"].mean(),
            "temp": df["temp"].mean(),
        }
        for name, df in turbines.items()
    ]
    header = f"""# Погода из Open-Meteo против SCADA

Сгенерировано `backend/src/analysis/weather_vs_scada.py`. Диагностика данных, а не
часть пайплайна: ERA5 используется только здесь и в прогноз не попадает.

Точка погоды: {LAT}, {LON} (середина между T1 и T2). SCADA приведена к часу:
среднее 10-минутных записей в окне ±30 мин вокруг часа, не меньше 4 записей из 6.

{md_table(coverage, COVERAGE_COLUMNS)}
"""
    sections = [
        header,
        summary,
        tz_text,
        height_section(site_utc, era5, nwp),
        skill_text,
        availability_text,
    ]
    return "\n".join(sections)


def write_report(report: str, path: Path) -> None:
    """Отчет с переводами строк LF на любой ОС, чтобы он побайтно совпадал с закоммиченным."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8", newline="\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--refresh", action="store_true", help="перекачать погоду из API вместо кэша")
    args = parser.parse_args()
    write_report(build_report(args.refresh), REPORT_PATH)
    print(f"report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
