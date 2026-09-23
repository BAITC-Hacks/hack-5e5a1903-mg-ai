"""SCADA: загрузка, часовой шаг, перевод в UTC, флаги (#9, #10)."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.forecast.dataset import config
from src.forecast.dataset.scada import (
    FLAG_CURTAILMENT,
    FLAG_CUT_OUT,
    FLAG_DOWNTIME,
    FLAG_FROZEN,
    FLAG_ICING,
    FLAG_NONE,
    FLAG_PRIORITY,
    ScadaFormatError,
    load_scada,
    passport_curve,
)
from src.forecast.dataset.scada_summary import build_summary

HEADER = "ID,Статистическое время,Средняя скорость ветра(m/s),Нормализованная активная мощность,Средняя температура окружающей среды(°C)"


def records(start: str, n: int = 6, wind=8.0, power=0.5, temp=10.0) -> list[tuple]:
    """``n`` записей с шагом 10 минут. Значения — число или список длины ``n``."""
    times = pd.date_range(start, periods=n, freq="10min")

    def values(v):
        return list(v) if isinstance(v, list | tuple) else [v] * n

    return list(zip(times, values(wind), values(power), values(temp), strict=True))


def write_turbine(path: Path, rows: list[tuple]) -> None:
    # Час без ведущего нуля, как в файлах организаторов: 2023-03-11 0:00:00.
    lines = [HEADER] + [f"{i},{t:%Y-%m-%d} {t.hour}:{t:%M:%S},{w},{p},{c}" for i, (t, w, p, c) in enumerate(rows, 1)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_scada(data_dir: Path, t1: list[tuple], t2: list[tuple] | None = None) -> Path:
    write_turbine(data_dir / config.SCADA_FILES["T1"], t1)
    write_turbine(data_dir / config.SCADA_FILES["T2"], t2 if t2 is not None else t1)
    return data_dir


def t1(history: pd.DataFrame) -> pd.DataFrame:
    return history[history["turbine"] == "T1"].set_index("time_utc")


def utc(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz="UTC")


# ---------------------------------------------------------------- агрегация


def test_hour_is_mean_of_its_records_labeled_by_hour_start(tmp_path):
    rows = records("2024-06-10 12:00", wind=[6, 7, 8, 9, 10, 11], power=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6], temp=[20, 20, 21, 21, 22, 22])
    history = t1(load_scada(write_scada(tmp_path, rows)))

    assert list(history.index) == [utc("2024-06-10 06:00")]
    hour = history.iloc[0]
    assert hour["wind_ms"] == pytest.approx(8.5)
    assert hour["power_norm"] == pytest.approx(0.35)
    assert hour["temp_c"] == pytest.approx(21.0)


def test_hour_needs_at_least_four_of_six_records(tmp_path):
    rows = records("2024-06-10 12:00", n=3) + records("2024-06-10 13:00", n=4) + records("2024-06-10 14:20", n=4)
    history = t1(load_scada(write_scada(tmp_path, rows)))

    # 12:00 — 3 записи, час отброшен. 13:00…13:30 и 14:20…14:50 — по 4 записи, часы засчитаны.
    assert list(history.index) == [utc("2024-06-10 07:00"), utc("2024-06-10 08:00")]


def test_duplicate_timestamps_counted_once(tmp_path):
    rows = records("2024-06-10 12:00", n=3, power=0.2)
    rows += records("2024-06-10 12:00", n=3, power=0.9)
    history = t1(load_scada(write_scada(tmp_path, rows)))

    # После удаления дубликатов в часе 3 записи, этого мало.
    assert history.empty


# ---------------------------------------------------------------- часовой пояс


def test_rule_from_tz_check_is_utc_plus_6():
    assert config.SCADA_UTC_OFFSET_H == 6


@pytest.mark.parametrize("local", ["2023-12-01 06:00", "2024-03-01 06:00", "2025-07-01 06:00"])
def test_load_scada_shifts_by_six_hours_on_whole_period(tmp_path, local):
    # И до, и после перехода области на UTC+5 01.03.2024: время SCADA − 6 ч.
    history = t1(load_scada(write_scada(tmp_path, records(local))))

    assert list(history.index) == [pd.Timestamp(local, tz="UTC") - pd.Timedelta(hours=6)]


def test_load_scada_takes_offset_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SCADA_UTC_OFFSET_H", 5)
    history = t1(load_scada(write_scada(tmp_path, records("2024-06-10 12:00"))))

    assert list(history.index) == [utc("2024-06-10 07:00")]


def test_time_utc_is_timezone_aware_utc(tmp_path):
    history = load_scada(write_scada(tmp_path, records("2024-06-10 12:00")))

    assert str(history["time_utc"].dt.tz) == "UTC"


# ---------------------------------------------------------------- флаги


@pytest.mark.parametrize(
    ("wind", "power", "temp", "expected"),
    [
        pytest.param(8.0, 0.45, 10.0, FLAG_NONE, id="clean"),
        pytest.param(8.0, 0.01, 10.0, FLAG_DOWNTIME, id="downtime"),
        pytest.param(3.0, 0.01, 10.0, FLAG_NONE, id="no-downtime-below-4ms"),
        pytest.param(13.0, 0.60, 10.0, FLAG_CURTAILMENT, id="curtailment"),
        pytest.param(10.5, 0.60, 10.0, FLAG_NONE, id="no-curtailment-below-11ms"),
        pytest.param(8.0, 0.10, -2.0, FLAG_ICING, id="icing"),
        pytest.param(8.0, 0.10, 5.0, FLAG_NONE, id="no-icing-when-warm"),
        pytest.param(8.0, 0.30, -2.0, FLAG_NONE, id="no-icing-above-half-curve"),
        pytest.param([8, 9, 26, 12, 10, 9], 0.9, 10.0, FLAG_CUT_OUT, id="cut-out"),
    ],
)
def test_flag(tmp_path, wind, power, temp, expected):
    history = t1(load_scada(write_scada(tmp_path, records("2024-06-10 12:00", wind=wind, power=power, temp=temp))))

    assert history["flag"].tolist() == [expected]


def test_curtailment_needs_power_below_threshold_whole_hour(tmp_path):
    # Средняя мощность 0,937 < 0,95, но одна запись дошла до номинала: это излом кривой, а не ограничение.
    rows = records("2024-06-10 12:00", wind=11.5, power=[0.92, 0.93, 0.97, 0.93, 0.92, 0.95])
    history = t1(load_scada(write_scada(tmp_path, rows)))

    assert history["power_norm"].iloc[0] < config.CURTAILMENT_MAX_POWER
    assert history["flag"].tolist() == [FLAG_NONE]


def test_frozen_sensor_flags_every_hour_of_the_run(tmp_path):
    # 18 записей подряд с одним ветром — ровно 3 часа, затем ветер меняется.
    rows = records("2024-06-10 12:00", n=18, wind=7.3) + records("2024-06-10 15:00", wind=[7, 8, 9, 8, 7, 8])
    history = t1(load_scada(write_scada(tmp_path, rows)))

    assert history["flag"].tolist() == [FLAG_FROZEN, FLAG_FROZEN, FLAG_FROZEN, FLAG_NONE]


def test_frozen_sensor_needs_three_hours(tmp_path):
    rows = records("2024-06-10 12:00", n=17, wind=7.3) + records("2024-06-10 14:50", n=7, wind=[8, 9, 8, 7, 8, 9, 8])
    history = t1(load_scada(write_scada(tmp_path, rows)))

    assert FLAG_FROZEN not in history["flag"].tolist()


def test_gap_breaks_frozen_run(tmp_path):
    # 9 одинаковых записей, дыра на час, еще 9 таких же: по отдельности серии короче 3 часов.
    rows = records("2024-06-10 12:00", n=9, wind=7.3) + records("2024-06-10 14:30", n=9, wind=7.3)
    history = t1(load_scada(write_scada(tmp_path, rows)))

    assert FLAG_FROZEN not in history["flag"].tolist()


def test_one_flag_per_hour_by_priority(tmp_path):
    # Мороз и мощность в ноль при ветре 8 м/с: это и простой, и обледенение. Остается обледенение.
    history = t1(load_scada(write_scada(tmp_path, records("2024-01-10 12:00", wind=8.0, power=0.01, temp=-5.0))))

    assert FLAG_PRIORITY.index(FLAG_ICING) < FLAG_PRIORITY.index(FLAG_DOWNTIME)
    assert history["flag"].tolist() == [FLAG_ICING]


def test_passport_curve():
    curve = passport_curve([0, 2.9, 3, 6, 10.3, 15, 24.9, 25, 30])

    assert curve[[0, 1, 2]].tolist() == [0.0, 0.0, 0.0]
    assert 0 < curve[3] < 1
    assert curve[[4, 5, 6]].tolist() == [1.0, 1.0, 1.0]
    assert curve[[7, 8]].tolist() == [0.0, 0.0]
    assert np.all(np.diff(passport_curve(np.linspace(3, 10.3, 50))) > 0)


# ---------------------------------------------------------------- формат ScadaHistory


def test_scada_history_format(tmp_path):
    t1_rows = records("2024-06-10 12:00", n=12, power=0.4) + records("2024-06-10 16:00", wind=8.0, power=0.01)
    t2_rows = records("2024-06-10 12:00", n=6, power=0.5)
    history = load_scada(write_scada(tmp_path, t1_rows, t2_rows))

    assert tuple(history.columns) == config.SCADA_COLUMNS
    assert set(history["turbine"]) == {"T1", "T2"}
    assert not history.duplicated(["time_utc", "turbine"]).any()
    assert history.equals(history.sort_values(["time_utc", "turbine"]).reset_index(drop=True))
    for column in ("power_norm", "wind_ms", "temp_c"):
        assert pd.api.types.is_float_dtype(history[column])
        assert history[column].notna().all()
    assert history["power_norm"].between(0, 1).all()
    assert set(history["flag"]) <= {*FLAG_PRIORITY, FLAG_NONE}
    assert history["flag"].tolist() == [FLAG_NONE, FLAG_NONE, FLAG_NONE, FLAG_DOWNTIME]


def test_load_scada_reads_data_dir_from_config_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", write_scada(tmp_path, records("2024-06-10 12:00")))

    assert len(load_scada()) == 2


def test_unknown_header_is_a_clear_error(tmp_path):
    write_scada(tmp_path, records("2024-06-10 12:00"))
    path = tmp_path / config.SCADA_FILES["T2"]
    path.write_text("ID,time,wind,power,temp\n1,2024-06-10 12:00:00,8,0.5,10\n", encoding="utf-8")

    with pytest.raises(ScadaFormatError, match="Статистическое время"):
        load_scada(tmp_path)


# ---------------------------------------------------------------- сводка


def test_summary_reports_gaps_and_flags(tmp_path):
    rows = records("2024-06-10 00:00", n=6 * 3) + records("2024-06-12 00:00", n=6, power=0.01)
    summary = build_summary(write_scada(tmp_path, rows))

    for section in ("## Покрытие", "## Пропуски по месяцам", "## Крупные дыры", "## Флаги"):
        assert section in summary
    # Дыра с 03:00 10.06 по 23:00 11.06 по времени SCADA, в UTC на 6 ч раньше: 45 часов.
    assert "| T1 | 09.06.2024 21:00 | 11.06.2024 17:00 | 45 | 1.9 |" in summary
    assert "| `downtime` |" in summary
