"""Сводка по SCADA: пропуски, доля флагов, крупные дыры.

Запуск из папки backend, сеть не нужна:

    uv run python -m src.forecast.dataset.scada_summary

Пишет ``reports/scada_summary.md`` (каталог задается переменной ``REPORTS_DIR``).
"""

import logging
from pathlib import Path

import pandas as pd

from src.forecast.dataset import config
from src.forecast.dataset.scada import (
    FLAG_CURTAILMENT,
    FLAG_CUT_OUT,
    FLAG_DOWNTIME,
    FLAG_FROZEN,
    FLAG_ICING,
    FLAG_NONE,
    FLAG_PRIORITY,
    aggregate_hourly,
    read_turbine_csv,
    turbine_hours,
)

logger = logging.getLogger(__name__)

REPORT_NAME = "scada_summary.md"
# Дыры в засчитанных часах от этой длины перечисляются в отчете поименно.
BIG_GAP_HOURS = 24

FLAG_MEANING = {
    FLAG_FROZEN: f"ветер не меняется {config.FROZEN_MIN_HOURS} ч и дольше",
    FLAG_CUT_OUT: f"ветер в часе достигал {config.CUT_OUT_MS:g} м/с",
    FLAG_ICING: f"t ≤ {config.ICING_MAX_TEMP_C:g} °C и мощность ниже {config.ICING_CURVE_SHARE:g} паспортной кривой",
    FLAG_DOWNTIME: (f"мощность ≤ {config.DOWNTIME_MAX_POWER:g} при ветре {config.DOWNTIME_WIND_MS[0]:g}–{config.DOWNTIME_WIND_MS[1]:g} м/с"),
    FLAG_CURTAILMENT: f"ветер > {config.CURTAILMENT_MIN_WIND_MS:g} м/с, а мощность весь час ниже {config.CURTAILMENT_MAX_POWER:g}",
    FLAG_NONE: "чистый час",
}


def md_table(header: list[str], rows: list[list]) -> str:
    """Markdown-таблица. Дробные числа печатаются с одним знаком после запятой."""

    def cell(value) -> str:
        return f"{value:.1f}" if isinstance(value, float) else str(value)

    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]
    return "\n".join(lines)


def pct(part: int, whole: int) -> float:
    return 100.0 * part / whole if whole else 0.0


def find_gaps(present: pd.DatetimeIndex, grid: pd.DatetimeIndex) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Непрерывные отрезки сетки ``grid``, которых нет в ``present``: (первый час, последний час, длина в часах)."""
    missing = grid.difference(present)
    if missing.empty:
        return []
    breaks = missing.to_series().diff().ne(pd.Timedelta(hours=1)).cumsum()
    return [(group.iloc[0], group.iloc[-1], len(group)) for _, group in missing.to_series().groupby(breaks)]


def build_summary(data_dir: Path | None = None) -> str:
    base = data_dir if data_dir is not None else config.DATA_DIR
    raws = {turbine: read_turbine_csv(base / name) for turbine, name in config.SCADA_FILES.items()}
    hours = {turbine: turbine_hours(raw) for turbine, raw in raws.items()}

    start = min(raw["time_local"].min() for raw in raws.values())
    end = max(raw["time_local"].max() for raw in raws.values())
    slots = pd.date_range(start, end, freq="10min")
    hour_grid_local = pd.date_range(start.floor("h"), end.floor("h"), freq="h")
    offset = pd.Timedelta(hours=config.SCADA_UTC_OFFSET_H)
    hour_grid = (hour_grid_local - offset).tz_localize("UTC")

    coverage_rows, gaps_rows, short_gaps = [], [], {}
    for turbine, raw in raws.items():
        all_hours = aggregate_hourly(raw)
        counted = len(hours[turbine])
        incomplete = int((all_hours["n_points"] < config.MIN_POINTS_PER_HOUR).sum())
        unique = raw["time_local"].nunique()
        coverage_rows.append(
            [
                turbine,
                len(raw),
                len(raw) - unique,
                pct(len(slots) - unique, len(slots)),
                len(hour_grid),
                counted,
                incomplete,
                len(hour_grid) - len(all_hours),
                pct(len(hour_grid) - counted, len(hour_grid)),
            ]
        )
        gaps = find_gaps(hours[turbine].index, hour_grid)
        short_gaps[turbine] = sum(1 for _, _, n in gaps if n < BIG_GAP_HOURS)
        for first, last, n in gaps:
            if n >= BIG_GAP_HOURS:
                gaps_rows.append([turbine, f"{first:%d.%m.%Y %H:%M}", f"{last:%d.%m.%Y %H:%M}", n, f"{n / 24:.1f}"])

    month = hour_grid.tz_localize(None).to_period("M")
    month_total = pd.Series(1, index=month).groupby(level=0).size()
    month_rows = []
    month_missing = {}
    for turbine, frame in hours.items():
        present = pd.Series(1, index=frame.index.tz_localize(None).to_period("M")).groupby(level=0).size()
        month_missing[turbine] = month_total.sub(present.reindex(month_total.index, fill_value=0))
    for period, total in month_total.items():
        month_rows.append([str(period), total, *(pct(int(month_missing[t][period]), total) for t in hours)])

    flag_rows = []
    for flag in (*FLAG_PRIORITY, FLAG_NONE):
        row = [f"`{flag}`" if flag else "—", FLAG_MEANING[flag]]
        for frame in hours.values():
            n = int((frame["flag"] == flag).sum())
            row += [n, pct(n, len(frame))]
        flag_rows.append(row)

    condition_rows = []
    for flag in FLAG_PRIORITY:
        row = [f"`{flag}`"]
        for frame in hours.values():
            row.append(int(frame[f"is_{flag}"].sum()))
        condition_rows.append(row)
    multi_row = ["несколько условий сразу"]
    for frame in hours.values():
        multi_row.append(int((frame[[f"is_{f}" for f in FLAG_PRIORITY]].sum(axis=1) > 1).sum()))
    condition_rows.append(multi_row)

    curtail_rows = []
    for turbine, frame in hours.items():
        windy = frame[frame["wind_ms"] > config.CURTAILMENT_MIN_WIND_MS]
        by_mean = int((windy["power_norm"] < config.CURTAILMENT_MAX_POWER).sum())
        by_max = int(frame[f"is_{FLAG_CURTAILMENT}"].sum())
        curtail_rows.append([turbine, len(windy), by_mean, pct(by_mean, len(windy)), by_max, pct(by_max, len(windy))])

    turbines = list(hours)
    first_utc, last_utc = hour_grid[0], hour_grid[-1]
    return f"""# SCADA: сводка по данным и флагам

Сгенерировано `uv run python -m src.forecast.dataset.scada_summary` (из `backend/`).
Код загрузки: `backend/src/forecast/dataset/scada.py`, пороги: `backend/src/forecast/dataset/config.py`.

- Период в файлах: {start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M} по времени SCADA.
- Перевод в UTC: `time_utc = время SCADA − {config.SCADA_UTC_OFFSET_H} ч` на всём периоде, проверка в `reports/tz_check.md`.
  В UTC это часы с {first_utc:%d.%m.%Y %H:%M} по {last_utc:%d.%m.%Y %H:%M}.
- Час — среднее 10-минутных записей hh:00…hh:50, метка — начало часа. Час засчитывается
  при {config.MIN_POINTS_PER_HOUR} записях из 6 и больше, остальные в `load_scada()` не попадают.

## Покрытие

{
        md_table(
            [
                "Турбина",
                "Записей 10 мин",
                "Дубликатов",
                "Пропущено записей, %",
                "Часов в периоде",
                "Засчитано часов",
                f"Неполных часов (< {config.MIN_POINTS_PER_HOUR} записей)",
                "Часов без записей",
                "Не засчитано часов, %",
            ],
            coverage_rows,
        )
    }

## Пропуски по месяцам

Доля часов месяца (UTC), которые не засчитаны: нет записей или их меньше {config.MIN_POINTS_PER_HOUR}.

{md_table(["Месяц", "Часов в периоде", *(f"{t}, %" for t in turbines)], month_rows)}

## Крупные дыры

Отрезки без засчитанных часов длиной от {BIG_GAP_HOURS} ч, время UTC, границы включительно.

{md_table(["Турбина", "Первый пропущенный час", "Последний пропущенный час", "Часов", "Суток"], gaps_rows)}

Дыр короче {BIG_GAP_HOURS} ч: {", ".join(f"{t} — {n}" for t, n in short_gaps.items())}.

## Флаги

Доля от засчитанных часов турбины. У часа один флаг: если выполнено несколько условий,
берется первое по порядку в таблице. Сначала то, что делает недостоверным сам замер,
затем более конкретная причина недовыработки, в конце более общие.

{md_table(["Флаг", "Условие", *(h for t in turbines for h in (f"{t}, часов", f"{t}, %"))], flag_rows)}

Сколько часов подходило под каждое условие до выбора одного флага:

{md_table(["Условие", *(f"{t}, часов" for t in turbines)], condition_rows)}

## Почему ограничение проверяется по всем записям часа

В #9 ограничение — «мощность стоит ниже {config.CURTAILMENT_MAX_POWER:g} при ветре выше {config.CURTAILMENT_MIN_WIND_MS:g} м/с».
Если сравнивать с порогом среднюю мощность часа, флаг ловит обычный излом кривой
на 11–12 м/с: порывы на несколько минут опускают мощность ниже номинала, и среднее
оказывается чуть ниже порога. Поэтому «стоит ниже» проверяется так: ни одна 10-минутная
запись часа порога не достигла.

{
        md_table(
            [
                "Турбина",
                f"Часов с ветром > {config.CURTAILMENT_MIN_WIND_MS:g} м/с",
                "По среднему часа",
                "%",
                "По всем записям часа (принято)",
                "%",
            ],
            curtail_rows,
        )
    }
"""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = build_summary()
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / REPORT_NAME
    path.write_text(report, encoding="utf-8", newline="\n")
    logger.info("report: %s", path)


if __name__ == "__main__":
    main()
