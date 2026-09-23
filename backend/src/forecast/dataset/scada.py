"""История турбин (SCADA): загрузка, часовой шаг, перевод в UTC, флаги очистки.

Точка входа — ``load_scada()``: таблица ScadaHistory из контракта #2, обе турбины
в длинном формате, одна строка на час и турбину.

Как 10-минутные записи становятся часом. В час hh попадают записи hh:00…hh:50,
значение — их среднее, метка — начало часа. Час засчитывается, если в нем есть хотя бы
4 записи из 6, иначе его в таблице нет. Такое среднее относится к интервалу
[hh:00, hh+1:00), то есть к энергии, выработанной за этот час.

Отличие от ``reports/weather_vs_scada.md`` и ``src/analysis/tz_check.py``: там часы
центрированы, в час hh идут записи hh−30…hh+20 мин. Прогноз погоды дает мгновенное
значение в hh:00, и для сравнения с ним нужен центр окна в hh:00. Здесь центр окна
в hh:30, это полчаса разницы при сопоставлении с погодой.

Флаги (пороги в ``config.py``). Если у часа выполнено несколько условий, остается одно
по приоритету ``FLAG_PRIORITY``: сначала то, что делает недостоверным сам замер
(замерзший датчик, срез по ветру), затем более конкретная причина недовыработки
(обледенение) и в конце более общие (простой, ограничение). Остановка при морозе
поэтому попадает в обледенение, а не в простой. Сколько часов подходило под несколько
условий сразу, показывает ``reports/scada_summary.md``.
"""

from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast.dataset import config

FLAG_NONE = ""
FLAG_FROZEN = "frozen_sensor"
FLAG_CUT_OUT = "cut_out"
FLAG_ICING = "icing"
FLAG_DOWNTIME = "downtime"
FLAG_CURTAILMENT = "curtailment"
FLAG_PRIORITY = (FLAG_FROZEN, FLAG_CUT_OUT, FLAG_ICING, FLAG_DOWNTIME, FLAG_CURTAILMENT)

# Русские заголовки файлов организаторов -> наши имена. Колонка ID не нужна.
CSV_HEADERS = {
    "Статистическое время": "time_local",
    "Средняя скорость ветра(m/s)": "wind_ms",
    "Нормализованная активная мощность": "power_norm",
    "Средняя температура окружающей среды(°C)": "temp_c",
}
MEASURES = ["wind_ms", "power_norm", "temp_c"]
STEP = pd.Timedelta(minutes=10)


class ScadaFormatError(ValueError):
    """Файл SCADA не того формата: нет ожидаемой колонки, время или значение не читается."""


def read_turbine_csv(path: Path) -> pd.DataFrame:
    """10-минутные записи одной турбины как есть, отсортированные по времени.

    Время остается локальным временем SCADA, без пояса. Дубликаты не удаляются,
    это делает ``aggregate_hourly``, чтобы сводка могла их посчитать. Пустая ячейка
    становится NaN, нечисловое значение — ошибкой формата.
    """
    raw = pd.read_csv(path)
    missing = [name for name in CSV_HEADERS if name not in raw.columns]
    if missing:
        raise ScadaFormatError(f"{path.name}: нет колонок {missing}, есть {list(raw.columns)}")
    raw = raw.rename(columns=CSV_HEADERS)[list(CSV_HEADERS.values())]
    try:
        raw["time_local"] = pd.to_datetime(raw["time_local"], format="%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ScadaFormatError(f"{path.name}: не читается время: {exc}") from exc
    for column in MEASURES:
        try:
            raw[column] = pd.to_numeric(raw[column]).astype(float)
        except ValueError as exc:
            raise ScadaFormatError(f"{path.name}: в {column} не число: {exc}") from exc
    return raw.sort_values("time_local", kind="stable").reset_index(drop=True)


def frozen_wind(raw: pd.DataFrame, min_hours: int = config.FROZEN_MIN_HOURS) -> pd.Series:
    """Записи, где ветер не меняется ``min_hours`` часов подряд и дольше.

    Серия — это соседние записи с шагом ровно 10 минут и одинаковым ветром.
    Разрыв в данных серию обрывает: одинаковые значения до и после дыры
    замерзшим датчиком не считаются.
    """
    continues = raw["wind_ms"].diff().eq(0) & raw["time_local"].diff().eq(STEP)
    run_id = (~continues).cumsum()
    run_len = run_id.map(run_id.value_counts())
    return run_len >= min_hours * 6


def aggregate_hourly(raw: pd.DataFrame) -> pd.DataFrame:
    """10 минут -> час с меткой начала часа. Возвращает все часы, где была хоть одна запись.

    Кроме средних считаются ``n_points`` (сколько записей в часе), ``power_max`` и
    ``wind_max`` по записям часа и ``frozen`` — была ли в часе запись из серии
    замерзшего датчика. Отсев неполных часов делает вызывающий код.

    Пустые ячейки в ``n_points`` не входят: это наименьшее по величинам число
    заполненных записей. Иначе час из четырех строк, где мощность есть только в одной,
    прошел бы проверку «4 из 6», а час из пустых строк стал бы чистым часом с NaN.
    """
    raw = raw.drop_duplicates("time_local").reset_index(drop=True)
    raw = raw.assign(frozen=frozen_wind(raw), hour=raw["time_local"].dt.floor("h"))
    grouped = raw.groupby("hour")
    hourly = grouped[MEASURES].mean()
    hourly["n_points"] = grouped[MEASURES].count().min(axis=1)
    hourly["power_max"] = grouped["power_norm"].max()
    hourly["wind_max"] = grouped["wind_ms"].max()
    hourly["frozen"] = grouped["frozen"].any()
    hourly.index.name = "time_local"
    return hourly


def to_utc(local: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Локальное время SCADA -> UTC по правилу ``config.SCADA_UTC_OFFSET_H``."""
    return (local - pd.Timedelta(hours=config.SCADA_UTC_OFFSET_H)).tz_localize("UTC")


def passport_curve(wind_ms) -> np.ndarray:
    """Паспортная кривая GW109 в долях номинала.

    Паспорт в #2 дает три точки: включение, номинал, отключение. Между включением
    и номиналом мощность растет как куб скорости ветра, как у энергии потока.
    """
    v = np.asarray(wind_ms, dtype=float)
    ramp = (v**3 - config.CUT_IN_MS**3) / (config.RATED_MS**3 - config.CUT_IN_MS**3)
    return np.select([v < config.CUT_IN_MS, v < config.RATED_MS, v < config.CUT_OUT_MS], [0.0, ramp, 1.0], 0.0)


def flag_conditions(hourly: pd.DataFrame) -> pd.DataFrame:
    """Какие условия флагов выполнены в каждом часе, до выбора одного по приоритету.

    Ограничение: мощность стоит ниже порога весь час, то есть ни одна 10-минутная
    запись его не достигла. Проверка по среднему за час ловит обычный излом кривой
    на 11–12 м/с, где порывы на минуты опускают мощность ниже номинала.
    """
    wind, power = hourly["wind_ms"], hourly["power_norm"]
    low, high = config.DOWNTIME_WIND_MS
    conditions = {
        FLAG_FROZEN: hourly["frozen"],
        FLAG_CUT_OUT: hourly["wind_max"] >= config.CUT_OUT_MS,
        FLAG_ICING: (hourly["temp_c"] <= config.ICING_MAX_TEMP_C) & (power < config.ICING_CURVE_SHARE * passport_curve(wind)),
        FLAG_DOWNTIME: (power <= config.DOWNTIME_MAX_POWER) & wind.between(low, high),
        FLAG_CURTAILMENT: (wind > config.CURTAILMENT_MIN_WIND_MS) & (hourly["power_max"] < config.CURTAILMENT_MAX_POWER),
    }
    return pd.DataFrame(conditions, index=hourly.index)[list(FLAG_PRIORITY)].astype(bool)


def assign_flag(conditions: pd.DataFrame) -> pd.Series:
    """Один флаг на час: первое выполненное условие по ``FLAG_PRIORITY``, иначе пусто."""
    flag = pd.Series(FLAG_NONE, index=conditions.index, dtype=object)
    for name in reversed(FLAG_PRIORITY):
        flag = flag.mask(conditions[name], name)
    return flag


def turbine_hours(raw: pd.DataFrame) -> pd.DataFrame:
    """Засчитанные часы одной турбины с UTC-меткой, условиями флагов и итоговым флагом."""
    hourly = aggregate_hourly(raw)
    hourly = hourly[hourly["n_points"] >= config.MIN_POINTS_PER_HOUR]
    conditions = flag_conditions(hourly)
    out = pd.concat([hourly, conditions.add_prefix("is_")], axis=1)
    out["flag"] = assign_flag(conditions)
    out.index = to_utc(out.index)
    out.index.name = "time_utc"
    return out


def load_scada(data_dir: Path | str | None = None) -> pd.DataFrame:
    """ScadaHistory: ``time_utc, turbine, power_norm, wind_ms, temp_c, flag``.

    Одна строка на засчитанный час и турбину, T1 и T2 вместе, сортировка по времени
    и турбине. ``time_utc`` — начало часа в UTC. ``flag`` — один из ``FLAG_PRIORITY``
    или пустая строка для чистого часа.
    """
    base = Path(data_dir) if data_dir is not None else config.DATA_DIR
    parts = []
    for turbine, name in config.SCADA_FILES.items():
        hours = turbine_hours(read_turbine_csv(base / name))
        parts.append(hours.reset_index().assign(turbine=turbine))
    history = pd.concat(parts, ignore_index=True)[list(config.SCADA_COLUMNS)]
    history["turbine"] = history["turbine"].astype(str)
    history["flag"] = history["flag"].astype(str)
    return history.sort_values(["time_utc", "turbine"], kind="stable").reset_index(drop=True)
