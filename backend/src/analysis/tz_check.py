"""Проверка часового пояса времени SCADA (#10).

Диагностика, а не часть пайплайна. Правило перевода живет в
``src/forecast/dataset/config.py`` (``SCADA_UTC_OFFSET_H``), а здесь оно проверяется:
почасовой ветер SCADA сравнивается с ветром из прогнозов погоды, время которых в UTC,
при гипотезах «время SCADA = UTC+k» для k в пределах ±3 ч от правила. Где корреляция
выше всего, там и пояс.

Эталоны:
- ECMWF IFS 0.25°, свежий прогон, 100 м — основной источник (#6);
- GFS, свежий прогон, 80 м — запасной источник, он же единственный прогноз до 05.03.2024;
- ERA5, 100 м — только дополнительная диагностика, нужен ради 2023 года, где прогнозов
  в кэше нет. В пайплайн прогноза ERA5 не попадает, поэтому модуль лежит вне ``src/forecast``.

Часы SCADA здесь центрированы: в час hh идут записи hh−30…hh+20 мин. У ``load_scada()``
метка — начало часа, и среднее относится к hh:30. При сравнении с мгновенным значением
погоды в hh:00 гипотезы UTC+5 и UTC+6 тогда отстоят от истины на одинаковые полчаса
и не различаются. Центрированное окно этот сдвиг убирает.

Сеть не нужна: читается закоммиченный кэш ``data/weather_compare/``. Запуск из backend:

    uv run python -m src.analysis.tz_check

Отчет: ``reports/tz_check.md``.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.forecast.dataset import config
from src.forecast.dataset.scada import read_turbine_csv

logger = logging.getLogger(__name__)

REPORT_NAME = "tz_check.md"
SHIFTS = range(-3, 4)
# Квартал с меньшим числом общих часов в таблицы не идет: корреляция на нем шумная.
MIN_PAIRS = 500
# Если другой сдвиг лучше правила меньше чем на столько, это ничья, а не опровержение:
# у кварталов, где правило выигрывает, запас бывает таким же маленьким.
TIE_MARGIN = 0.01
# Смена часов SCADA сдвинула бы пик корреляции температуры на 1 ч от его медианы по кварталам.
# Отход в ту сторону, куда указывает ветер, больше этого порога считается сменой часов.
TEMP_PEAK_TOLERANCE_H = 0.5
# Смена пояса Алматинской области: с 01.03.2024 UTC+5 вместо UTC+6.
TZ_SWITCH = pd.Timestamp("2024-03-01")
MAIN_SOURCES = ("ifs", "gfs")


@dataclass(frozen=True)
class Reference:
    key: str
    label: str
    role: str
    file: str
    wind_column: str


REFERENCES = (
    Reference("ifs", "ECMWF IFS 0.25°, 100 м", "основной источник", "prev_runs_ecmwf_ifs025.csv.gz", "wind_speed_100m"),
    Reference("gfs", "GFS, 80 м", "запасной источник", "prev_runs_gfs_global.csv.gz", "wind_speed_80m"),
    Reference("era5", "ERA5, 100 м", "доп. диагностика", "era5.csv.gz", "wind_speed_100m"),
)
SHORT = {"ifs": "IFS", "gfs": "GFS", "era5": "ERA5"}


def centered_hourly(raw: pd.DataFrame, column: str) -> pd.Series:
    """Одна величина одной турбины: среднее записей hh−30…hh+20 мин, не меньше 4 из 6. Время SCADA."""
    raw = raw.drop_duplicates("time_local")
    hour = (raw["time_local"] + pd.Timedelta(minutes=30)).dt.floor("h")
    grouped = raw.groupby(hour)[column]
    series = grouped.mean()[grouped.count() >= config.MIN_POINTS_PER_HOUR]
    series.index.name = "time_local"
    return series


def site_series(raws: dict[str, pd.DataFrame], column: str) -> pd.Series:
    """Среднее двух турбин по центрированным часам. Если одной турбины нет, берется другая."""
    parts = {turbine: centered_hourly(raw, column) for turbine, raw in raws.items()}
    return pd.concat(parts, axis=1, sort=True).mean(axis=1).rename(column)


def load_reference(data_dir: Path, ref: Reference) -> pd.DataFrame:
    path = data_dir / "weather_compare" / ref.file
    if not path.exists():
        raise FileNotFoundError(f"нет кэша погоды {path}")
    frame = pd.read_csv(path, parse_dates=["time"], index_col="time")
    return frame.rename(columns={ref.wind_column: "wind_ms", "temperature_2m": "temp_c"})[["wind_ms", "temp_c"]]


def offset_correlations(local: pd.Series, utc: pd.Series, offsets) -> pd.Series:
    """Корреляция при гипотезе «время SCADA = UTC+k», то есть UTC = время SCADA − k ч."""
    out = {}
    for k in offsets:
        shifted = local.copy()
        shifted.index = shifted.index - pd.Timedelta(hours=k)
        joined = pd.concat([shifted, utc], axis=1, join="inner").dropna()
        out[k] = joined.iloc[:, 0].corr(joined.iloc[:, 1])
    return pd.Series(out, dtype=float)


def peak_estimate(corrs: pd.Series) -> float:
    """Положение пика корреляции с точностью до долей часа: парабола через лучший сдвиг и соседей."""
    best = int(corrs.idxmax())
    if best - 1 not in corrs.index or best + 1 not in corrs.index:
        return float(best)
    left, mid, right = corrs[best - 1], corrs[best], corrs[best + 1]
    curvature = left - 2 * mid + right
    return float(best) if curvature == 0 else best + 0.5 * (left - right) / curvature


def pairs_at(local: pd.Series, utc: pd.Series, offset: int) -> int:
    shifted = local.dropna().index - pd.Timedelta(hours=offset)
    return int(shifted.isin(utc.dropna().index).sum())


def check_rows(local: pd.Series, utc: pd.Series, groups: dict[str, pd.Series], offsets: list[int]) -> list[dict]:
    """По каждой группе часов SCADA: число общих часов, корреляция на каждом сдвиге, лучший сдвиг, запас."""
    rows = []
    for name, part in groups.items():
        n = pairs_at(part, utc, config.SCADA_UTC_OFFSET_H)
        if n < MIN_PAIRS:
            continue
        corrs = offset_correlations(part, utc, offsets)
        ordered = corrs.sort_values(ascending=False)
        rows.append(
            {
                "group": name,
                "n": n,
                "corrs": corrs,
                "best": int(ordered.index[0]),
                "margin": float(ordered.iloc[0] - ordered.iloc[1]),
                "peak": peak_estimate(corrs),
            }
        )
    return rows


def quarters(local: pd.Series) -> dict[str, pd.Series]:
    return {f"{y} Q{q}": part for (y, q), part in local.groupby([local.index.year, local.index.quarter])}


def periods(local: pd.Series) -> dict[str, pd.Series]:
    return {
        f"до {TZ_SWITCH:%d.%m.%Y}": local[local.index < TZ_SWITCH],
        f"с {TZ_SWITCH:%d.%m.%Y}": local[local.index >= TZ_SWITCH],
    }


def classify(rows: list[dict]) -> tuple[list[str], list[str]]:
    """Кварталы, где правило проиграло: (ничьи с запасом меньше TIE_MARGIN, настоящие опровержения)."""
    rule = config.SCADA_UTC_OFFSET_H
    lost = [r for r in rows if r["best"] != rule]
    return [r["group"] for r in lost if r["margin"] < TIE_MARGIN], [r["group"] for r in lost if r["margin"] >= TIE_MARGIN]


def verdict(rows: list[dict]) -> str:
    rule = config.SCADA_UTC_OFFSET_H
    ties, contradictions = classify(rows)
    won = len(rows) - len(ties) - len(contradictions)
    text = f"UTC+{rule} лучший в {won} из {len(rows)} кварталов"
    if ties:
        text += f"; ничья (запас < {TIE_MARGIN:g}): {', '.join(ties)}"
    if contradictions:
        text += f"; опровергают: {', '.join(contradictions)}"
    return text


def disputed_directions(wind_rows: dict[str, list[dict]]) -> dict[str, int]:
    """Спорные кварталы и куда указывает ветер: −1, если лучше меньший сдвиг, +1 — если больший."""
    rule = config.SCADA_UTC_OFFSET_H
    votes: dict[str, list[int]] = {}
    for rows in wind_rows.values():
        for row in rows:
            if row["best"] != rule:
                votes.setdefault(row["group"], []).append(int(np.sign(row["best"] - rule)))
    return {group: int(np.sign(sum(v))) or v[0] for group, v in sorted(votes.items())}


def temp_shift_check(temp_rows: dict[str, list[dict]], directions: dict[str, int]) -> list[dict]:
    """Для спорных кварталов: отход пика температуры от медианы по всем кварталам, по каждому источнику.

    Если бы часы SCADA перевели на час в сторону, куда указывает ветер, пик температуры
    сместился бы туда же примерно на час. ``moved`` — сдвиг в эту сторону больше допуска.
    """
    out = []
    for key, rows in temp_rows.items():
        peaks = {r["group"]: r["peak"] for r in rows}
        median = float(np.median(list(peaks.values())))
        for group, direction in directions.items():
            if group in peaks:
                delta = peaks[group] - median
                out.append(
                    {
                        "source": key,
                        "group": group,
                        "peak": peaks[group],
                        "median": median,
                        "delta": delta,
                        "expected": direction,
                        "moved": delta * direction > TEMP_PEAK_TOLERANCE_H,
                    }
                )
    return out


def corr_table(rows: list[dict], offsets: list[int], first_title: str) -> str:
    rule = config.SCADA_UTC_OFFSET_H
    head = [first_title, "Часов", *(f"**UTC+{k}**" if k == rule else f"UTC+{k}" for k in offsets), "Лучший", "Запас"]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for row in rows:
        cells = [f"{row['corrs'][k]:.3f}" for k in offsets]
        best = f"UTC+{row['best']}" if row["best"] == rule else f"**UTC+{row['best']}**"
        lines.append("| " + " | ".join([row["group"], str(row["n"]), *cells, best, f"{row['margin']:.3f}"]) + " |")
    return "\n".join(lines)


def peak_table(wind_rows: dict[str, list[dict]], temp_rows: dict[str, list[dict]]) -> str:
    keys = [ref.key for ref in REFERENCES]
    peaks = {("wind", k): {r["group"]: r["peak"] for r in wind_rows[k]} for k in keys}
    peaks |= {("temp", k): {r["group"]: r["peak"] for r in temp_rows[k]} for k in keys}
    groups = sorted({g for column in peaks.values() for g in column})
    head = ["Квартал", *(f"ветер, {SHORT[k]}" for k in keys), *(f"t, {SHORT[k]}" for k in keys)]
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    for g in groups:
        cells = [f"{peaks[(kind, k)][g]:.2f}" if g in peaks[(kind, k)] else "—" for kind in ("wind", "temp") for k in keys]
        lines.append("| " + " | ".join([g, *cells]) + " |")
    return "\n".join(lines)


def build_report(data_dir: Path | None = None) -> str:
    base = data_dir if data_dir is not None else config.DATA_DIR
    rule = config.SCADA_UTC_OFFSET_H
    offsets = [rule + d for d in SHIFTS]
    raws = {turbine: read_turbine_csv(base / name) for turbine, name in config.SCADA_FILES.items()}
    wind, temp = site_series(raws, "wind_ms"), site_series(raws, "temp_c")
    refs = {ref.key: load_reference(base, ref) for ref in REFERENCES}

    wind_rows, period_rows, temp_rows = {}, {}, {}
    for key, frame in refs.items():
        wind_rows[key] = check_rows(wind, frame["wind_ms"].dropna(), quarters(wind), offsets)
        period_rows[key] = check_rows(wind, frame["wind_ms"].dropna(), periods(wind), offsets)
        # У температуры датчик запаздывает примерно на час, поэтому окно сдвигов шире вверх.
        temp_rows[key] = check_rows(temp, frame["temp_c"].dropna(), quarters(temp), [*offsets, offsets[-1] + 1])

    contradictions = sorted({g for key in MAIN_SOURCES for g in classify(wind_rows[key])[1]})
    directions = disputed_directions(wind_rows)
    disputed = list(directions)
    temp_check = temp_shift_check(temp_rows, directions)
    temp_moved = sorted({r["group"] for r in temp_check if r["moved"]})

    if not contradictions and not temp_moved:
        conclusion = (
            f"**Правило подтверждено: время SCADA — UTC+{rule} на всём периоде, `time_utc = время SCADA − {rule} ч`.** "
            f"Переход области на UTC+5 {TZ_SWITCH:%d.%m.%Y} в данных не отражен: и до, и после этой даты лучший сдвиг UTC+{rule}."
        )
        if disputed:
            conclusion += (
                f" В кварталах {', '.join(disputed)} по ветру ничья между соседними сдвигами, "
                "а пик температуры на месте, то есть часы не переводили (раздел «Спорные кварталы»)."
            )
    else:
        conclusion = f"**Правило UTC+{rule} подтверждено не везде**, спорные кварталы: {', '.join([*contradictions, *temp_moved])}. Разбор ниже."

    if temp_moved:
        temp_outcome = f"Пик температуры сдвинулся в сторону, куда указывает ветер: {', '.join(temp_moved)}."
    else:
        temp_outcome = "Пик температуры в сторону, куда указывает ветер, не сдвинулся: смены часов нет, спор по ветру — шум квартала."
    if disputed:
        temp_lines = "\n".join(
            f"| {r['group']} | {SHORT[r['source']]} | {r['peak']:.2f} | {r['median']:.2f} | {r['delta']:+.2f} | {r['expected']:+d} |"
            for r in temp_check
        )
        disputed_text = f"""## Спорные кварталы

Кварталы, где хотя бы по одному источнику другой сдвиг оказался лучше правила:
{", ".join(disputed)}. Во всех случаях, где это ничья, соседние сдвиги почти равны:
пик корреляции ветра лежит между двумя целыми часами (таблица выше).

Независимая проверка — температура. У нее сильный суточный ход, и пик корреляции
держится на одном месте весь период (он на час больше, чем у ветра: датчик на турбине
прогревается и остывает с запаздыванием). Если бы часы SCADA в этом квартале перевели
туда, куда указывает ветер, пик температуры сместился бы от своей медианы примерно
на час в ту же сторону (колонка «Ждем при смене часов»). Смена часов засчитывается,
если отход в эту сторону больше {TEMP_PEAK_TOLERANCE_H:g} ч.

| Квартал | Источник | Пик температуры, ч | Медиана по кварталам, ч | Отход, ч | Ждем при смене часов, ч |
|---|---|---|---|---|---|
{temp_lines}

{temp_outcome}
"""
    else:
        disputed_text = ""

    sections = []
    for ref in REFERENCES:
        frame = refs[ref.key]["wind_ms"].dropna()
        sections.append(
            f"""### {ref.label} — {ref.role}

Кэш `data/weather_compare/{ref.file}`, колонка `{ref.wind_column}`, данные с {frame.index.min():%d.%m.%Y} по {frame.index.max():%d.%m.%Y}.
Итог: {verdict(wind_rows[ref.key])}.

До и после перехода области на UTC+5:

{corr_table(period_rows[ref.key], offsets, "Период")}

По кварталам:

{corr_table(wind_rows[ref.key], offsets, "Квартал")}
"""
        )

    lines = "\n".join(f"- {ref.label} ({ref.role}): {verdict(wind_rows[ref.key])}." for ref in REFERENCES)
    return f"""# Часовой пояс времени SCADA

Сгенерировано `uv run python -m src.analysis.tz_check` (из `backend/`), сеть не нужна.
Правило в коде: `SCADA_UTC_OFFSET_H = {rule}` в `backend/src/forecast/dataset/config.py`,
его применение в `load_scada()` проверяет тест `backend/tests/forecast/test_scada.py`.

## Вывод

{conclusion}

По ветру:

{lines}

## Метод

Средний ветер двух турбин за час сравнивается с ветром из свежего прогона погоды на тот же
час UTC. Для каждой гипотезы «время SCADA = UTC+k», k от UTC+{offsets[0]} до UTC+{offsets[-1]},
время SCADA переводится в UTC как `время − k ч` и считается корреляция Пирсона. Правильный
сдвиг дает самую высокую корреляцию: суточный ход ветра и прохождение фронтов совпадают
по времени.

- Часы SCADA центрированы: среднее записей hh−30…hh+20 мин, не меньше {config.MIN_POINTS_PER_HOUR} из 6.
  Прогноз дает мгновенное значение в hh:00, и окно с центром в hh:00 не вносит своего
  сдвига на полчаса, как метка начала часа в `load_scada()`.
- Выбранный источник — ECMWF IFS (#6). В кэше Previous Runs он есть на сетке 0,25°, это та же
  модель. Для проверки времени этого достаточно: сдвиг виден по ходу ветра во времени, а не по его уровню.
- IFS в кэше начинается с марта 2024, GFS с января 2024. Для 2023 года прогнозов нет, поэтому
  добавлен ERA5. Он используется только в этой диагностике и в прогноз не попадает.
- Квартал, где при сдвиге UTC+{rule} меньше {MIN_PAIRS} общих часов, в таблицы не попадает.
  Кварталы берутся по времени SCADA.
- «Запас» — насколько корреляция лучшего сдвига выше второй по величине. Если правило
  проиграло с запасом меньше {TIE_MARGIN:g}, это ничья: у кварталов, где правило выигрывает,
  запас бывает таким же.

## Положение пика по кварталам

Сдвиг k, при котором корреляция максимальна, с долями часа: парабола через лучший целый
сдвиг и два соседних. Для ветра правило UTC+{rule} означает пик около {rule}. У температуры пик
на час больше из-за запаздывания датчика.

{peak_table(wind_rows, temp_rows)}

{disputed_text}
## Корреляция ветра по источникам

{"".join(sections)}"""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    report = build_report()
    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.REPORTS_DIR / REPORT_NAME
    path.write_text(report, encoding="utf-8", newline="\n")
    logger.info("report: %s", path)


if __name__ == "__main__":
    main()
