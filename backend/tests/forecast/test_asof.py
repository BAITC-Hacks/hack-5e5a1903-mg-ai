import logging
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from src.forecast.weather import asof as asof_module
from src.forecast.weather.asof import CACHE_TO_OUTPUT, OUTPUT_COLUMNS, AsOfStore, LeakageError, NoRunAvailable, RunAvailable, check_no_leakage
from src.forecast.weather.sources import SOURCES
from tests.forecast.synthetic_nwp import synthetic_cache, write_cache

START, END = "2026-01-10", "2026-02-06"
UTC = "UTC"


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz=UTC)


def horizon(as_of: pd.Timestamp, hours: int = 48) -> pd.DatetimeIndex:
    return pd.date_range(as_of.floor("h") + pd.Timedelta(hours=1), periods=hours, freq="h")


@pytest.fixture(scope="module")
def caches():
    return {name: synthetic_cache(source, START, END, seed=i) for i, (name, source) in enumerate(SOURCES.items())}


@pytest.fixture(scope="module")
def cache_root(tmp_path_factory, caches):
    root = tmp_path_factory.mktemp("nwp")
    for name, frame in caches.items():
        write_cache(frame, root, SOURCES[name])
    return root


@pytest.fixture
def store(cache_root):
    return AsOfStore(cache_root)


@pytest.fixture
def asof_logs(caplog):
    """Логгер ``src`` не пробрасывает записи в корень, поэтому вешаем caplog прямо на логгер модуля."""
    asof_module.logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger=asof_module.logger.name)
    yield caplog
    asof_module.logger.removeHandler(caplog.handler)


def reference_choice(cache: pd.DataFrame, source: str, as_of: pd.Timestamp, valid_times: pd.DatetimeIndex) -> pd.Series:
    """Прямой перебор: для каждого часа самый поздний прогон, доступный к as_of."""
    delay = SOURCES[source].delay
    runs = pd.to_datetime(cache["run_init_utc"], utc=True)
    valid = pd.to_datetime(cache["valid_time_utc"], utc=True)
    step_ok = (runs - runs.dt.floor("D")) % pd.Timedelta(hours=SOURCES[source].run_step_h) == pd.Timedelta(0)
    chosen = {}
    for hour in valid_times:
        candidates = runs[(valid == hour) & (runs + delay <= as_of) & step_ok]
        chosen[hour] = candidates.max()
    return pd.Series(chosen)


def random_as_of(seed: int, count: int) -> list[pd.Timestamp]:
    rng = np.random.default_rng(seed)
    minutes = rng.integers(0, int((ts("2026-02-03") - ts("2026-01-13")) / pd.Timedelta(minutes=1)), count)
    return [ts("2026-01-13") + pd.Timedelta(minutes=int(m)) for m in minutes]


def boundary_as_of(source: str) -> list[pd.Timestamp]:
    """Ровно в момент доступности прогона и за минуту до него."""
    available = ts("2026-01-20 00:00") + SOURCES[source].delay
    return [available, available - pd.Timedelta(minutes=1)]


RANDOM_AS_OF = random_as_of(seed=42, count=10)


@pytest.mark.parametrize("source", list(SOURCES))
@pytest.mark.parametrize("as_of", [*RANDOM_AS_OF, "published", "minute_before"], ids=str)
def test_asof_never_returns_future_runs(store, caches, source, as_of):
    if as_of == "published":
        as_of = boundary_as_of(source)[0]
    elif as_of == "minute_before":
        as_of = boundary_as_of(source)[1]
    valid_times = pd.date_range(as_of.floor("h") - pd.Timedelta(hours=24), periods=73, freq="h")

    out = store.get_nwp(source, as_of, valid_times)

    assert list(out.columns) == OUTPUT_COLUMNS
    assert (out["available_at_utc"] <= as_of).all()
    assert (out["available_at_utc"] == out["run_init_utc"] + SOURCES[source].delay).all()
    assert (out["lead_h"] == (out["valid_time_utc"] - out["run_init_utc"]) / pd.Timedelta(hours=1)).all()
    assert (out["source"] == source).all()
    assert list(out["valid_time_utc"]) == list(valid_times)
    expected = reference_choice(caches[source], source, as_of, valid_times)
    assert list(out["run_init_utc"]) == list(expected)


@pytest.mark.parametrize("source", list(SOURCES))
def test_canary_future_runs_replaced_with_garbage(tmp_path, caches, source):
    t0 = ts("2026-01-25 02:00")
    clean = caches[source]
    poisoned = clean.copy()
    future = poisoned["run_init_utc"] + SOURCES[source].delay > t0
    rng = np.random.default_rng(99)
    value_columns = list(CACHE_TO_OUTPUT)
    poisoned.loc[future, value_columns] = rng.uniform(-1e6, 1e6, (int(future.sum()), len(value_columns)))
    write_cache(clean, tmp_path / "clean", SOURCES[source])
    write_cache(poisoned, tmp_path / "poisoned", SOURCES[source])
    valid_times = pd.date_range(t0 - pd.Timedelta(hours=72), t0 + pd.Timedelta(hours=48), freq="h")

    a = AsOfStore(tmp_path / "clean").get_nwp(source, t0, valid_times)
    b = AsOfStore(tmp_path / "poisoned").get_nwp(source, t0, valid_times)

    assert future.sum() > 0
    assert a.to_csv(index=False).encode() == b.to_csv(index=False).encode()
    assert (pd.util.hash_pandas_object(a, index=False) == pd.util.hash_pandas_object(b, index=False)).all()


def test_ifs_example_issue_2026_01_31(store):
    as_of = ts("2026-01-31 02:00")
    out = store.get_nwp("ifs", as_of, horizon(as_of))

    assert (out["run_init_utc"] == ts("2026-01-30 18:00")).all()
    assert (out["available_at_utc"] == ts("2026-01-31 01:30")).all()
    assert out["lead_h"].iloc[0] == 9
    assert out["lead_h"].iloc[-1] == 56


def test_ifs_switches_to_00z_exactly_when_it_is_published(store):
    valid_times = horizon(ts("2026-01-31 02:00"))
    before = store.get_nwp("ifs", ts("2026-01-31 07:29"), valid_times)
    after = store.get_nwp("ifs", ts("2026-01-31 07:30"), valid_times)

    assert (before["run_init_utc"] == ts("2026-01-30 18:00")).all()
    assert (after["run_init_utc"] == ts("2026-01-31 00:00")).all()


def test_gem_has_only_00z_and_12z_runs(tmp_path, caches, store, asof_logs):
    gem = SOURCES["gem"]
    fake = synthetic_cache(replace(gem, run_step_h=6), START, END, seed=7)
    fake = fake[fake["run_init_utc"].dt.hour.isin([6, 18])]
    write_cache(pd.concat([caches["gem"], fake], ignore_index=True), tmp_path, gem)
    polluted = AsOfStore(tmp_path)

    for as_of in random_as_of(seed=5, count=10):
        out = polluted.get_nwp("gem", as_of, horizon(as_of))
        assert set(out["run_init_utc"].dt.hour) <= {0, 12}
    events = polluted.run_events(ts("2026-01-20"), ts("2026-01-21"), ["gem"])
    assert [(e.run_init_utc.hour, e.available_at_utc) for e in events] == [(0, ts("2026-01-20 08:00")), (12, ts("2026-01-20 20:00"))]
    assert "вне шага 12 ч" in asof_logs.text
    assert set(store.get_nwp("gem", ts("2026-01-31 02:00"), horizon(ts("2026-01-31 02:00")))["run_init_utc"].dt.hour) <= {0, 12}


def test_no_run_before_first_publication(store):
    as_of = ts(START) + pd.Timedelta(hours=1)
    with pytest.raises(NoRunAvailable) as exc:
        store.get_nwp("ifs", as_of, horizon(as_of))
    assert exc.value.source == "ifs"
    assert len(exc.value.missing) == 48


def test_no_run_for_part_of_the_hours(store):
    as_of = ts("2026-02-05 12:00")
    with pytest.raises(NoRunAvailable) as exc:
        store.get_nwp("gfs", as_of, horizon(as_of))
    assert 0 < len(exc.value.missing) < 48
    assert exc.value.missing.min() > ts(END)


def test_no_run_for_empty_cache(tmp_path):
    with pytest.raises(NoRunAvailable):
        AsOfStore(tmp_path).get_nwp("ifs", ts("2026-01-31 02:00"), horizon(ts("2026-01-31 02:00")))


def test_lead_h_for_previous_runs(store):
    as_of = ts("2026-01-31 02:00")
    out = store.get_nwp("gfs", as_of, [ts("2026-01-31 03:00"), ts("2026-02-01 19:00")])

    assert list(out["run_init_utc"]) == [ts("2026-01-30 00:00"), ts("2026-01-30 18:00")]
    assert list(out["lead_h"]) == [27, 49]


def test_empty_columns_stay_nan(store):
    out = store.get_nwp("gem", ts("2026-01-31 02:00"), horizon(ts("2026-01-31 02:00")))
    assert out["ws100"].isna().all()
    assert out["psfc"].isna().all()
    assert out["ws80"].notna().all()


WIND_CACHE_COLUMNS = ["wind_speed_80m", "wind_speed_100m", "wind_speed_120m"]


def ifs_with_blank_wind(caches, runs: list[str], columns: list[str]) -> pd.DataFrame:
    frame = caches["ifs"].copy()
    frame.loc[frame["run_init_utc"].isin([ts(run) for run in runs]), columns] = np.nan
    return frame


def test_run_without_wind_falls_back_to_older_run(tmp_path, caches, asof_logs):
    """Сбой архива, как у IFS 04–09.08.2025: у свежего прогона ветер пуст на всех высотах."""
    write_cache(ifs_with_blank_wind(caches, ["2026-01-30 18:00"], WIND_CACHE_COLUMNS), tmp_path, SOURCES["ifs"])
    as_of = ts("2026-01-31 02:00")

    out = AsOfStore(tmp_path).get_nwp("ifs", as_of, horizon(as_of))

    assert (out["run_init_utc"] == ts("2026-01-30 12:00")).all()
    assert out["ws80"].notna().all()
    assert "без скорости ветра" in asof_logs.text


def test_run_with_wind_at_some_height_is_kept(tmp_path, caches):
    write_cache(ifs_with_blank_wind(caches, ["2026-01-30 18:00"], ["wind_speed_80m"]), tmp_path, SOURCES["ifs"])
    as_of = ts("2026-01-31 02:00")

    out = AsOfStore(tmp_path).get_nwp("ifs", as_of, horizon(as_of))

    assert (out["run_init_utc"] == ts("2026-01-30 18:00")).all()
    assert out["ws80"].isna().all()
    assert out["ws100"].notna().all()


def test_no_run_with_wind_is_reported_not_returned_as_nan(tmp_path, caches):
    runs = ["2026-01-30 00:00", "2026-01-30 06:00", "2026-01-30 12:00", "2026-01-30 18:00"]
    write_cache(ifs_with_blank_wind(caches, runs, WIND_CACHE_COLUMNS), tmp_path, SOURCES["ifs"])
    as_of = ts("2026-01-31 02:00")

    with pytest.raises(NoRunAvailable) as exc:
        AsOfStore(tmp_path).get_nwp("ifs", as_of, horizon(as_of))
    assert list(exc.value.missing) == list(pd.date_range("2026-02-01 19:00", "2026-02-02 02:00", freq="h", tz=UTC))


def test_multi_skips_source_without_runs(tmp_path, caches, asof_logs):
    for name in ("ifs", "gfs"):
        write_cache(caches[name], tmp_path, SOURCES[name])
    as_of = ts("2026-01-31 02:00")

    out = AsOfStore(tmp_path).get_nwp_multi(["ifs", "icon", "gfs"], as_of, horizon(as_of))

    assert list(out["source"].unique()) == ["ifs", "gfs"]
    assert len(out) == 96
    assert (out["available_at_utc"] <= as_of).all()
    assert "Источник icon пропущен" in asof_logs.text


def test_multi_all_sources(store):
    as_of = ts("2026-01-31 02:00")
    out = store.get_nwp_multi(list(SOURCES), as_of, horizon(as_of))
    assert out.groupby("source").size().to_dict() == {name: 48 for name in SOURCES}


def test_multi_without_any_source_raises(tmp_path):
    with pytest.raises(NoRunAvailable) as exc:
        AsOfStore(tmp_path).get_nwp_multi(["ifs", "gfs"], ts("2026-01-31 02:00"), horizon(ts("2026-01-31 02:00")))
    assert exc.value.source == ["ifs", "gfs"]


def test_run_events_sorted_and_half_open(store):
    events = store.run_events(ts("2026-01-31 01:30"), ts("2026-01-31 08:00"), ["ifs", "gfs", "gem", "icon", "ifs025"])

    assert events == sorted(events, key=lambda e: (e.available_at_utc, e.source, e.run_init_utc))
    assert all(ts("2026-01-31 01:30") < e.available_at_utc <= ts("2026-01-31 08:00") for e in events)
    assert RunAvailable("ifs", ts("2026-01-31 00:00"), ts("2026-01-31 07:30")) in events
    assert RunAvailable("gfs", ts("2026-01-31 00:00"), ts("2026-01-31 07:00")) in events
    assert RunAvailable("gem", ts("2026-01-31 00:00"), ts("2026-01-31 08:00")) in events
    assert not any(e.source == "ifs" and e.run_init_utc == ts("2026-01-30 18:00") for e in events)


def test_run_events_for_ifs_day(store):
    events = store.run_events(ts("2026-01-31"), ts("2026-02-01"), ["ifs"])
    assert [e.available_at_utc.strftime("%H:%M") for e in events] == ["01:30", "07:30", "13:30", "19:30"]


def test_final_guard_catches_leak_from_selection_bug(store):
    as_of = ts("2026-01-31 02:00")
    store.get_nwp("ifs", as_of, horizon(as_of))
    frame = store._data["ifs"].frame
    frame["available_at_utc"] = frame["available_at_utc"] + pd.Timedelta(days=1)

    with pytest.raises(LeakageError):
        store.get_nwp("ifs", as_of, horizon(as_of))


def test_check_no_leakage():
    as_of = ts("2026-01-31 02:00")
    frame = pd.DataFrame(
        {
            "source": ["ifs", "ifs"],
            "run_init_utc": [ts("2026-01-30 18:00"), ts("2026-01-31 00:00")],
            "available_at_utc": [ts("2026-01-31 01:30"), ts("2026-01-31 07:30")],
        }
    )
    check_no_leakage(frame.iloc[:1], as_of)
    with pytest.raises(LeakageError, match="1 строк"):
        check_no_leakage(frame, as_of)


def test_naive_times_are_rejected(store):
    with pytest.raises(ValueError, match="часового пояса"):
        store.get_nwp("ifs", pd.Timestamp("2026-01-31 02:00"), horizon(ts("2026-01-31 02:00")))
    with pytest.raises(ValueError, match="часового пояса"):
        store.get_nwp("ifs", ts("2026-01-31 02:00"), pd.date_range("2026-01-31 03:00", periods=3, freq="h"))


def test_unknown_source(store):
    with pytest.raises(ValueError, match="Неизвестный источник"):
        store.get_nwp("era5", ts("2026-01-31 02:00"), horizon(ts("2026-01-31 02:00")))


def test_cache_is_loaded_once(store, monkeypatch):
    calls = []
    original = asof_module.load_source_cache
    monkeypatch.setattr(asof_module, "load_source_cache", lambda *args: calls.append(args) or original(*args))
    for day in range(1, 4):
        as_of = ts(f"2026-01-2{day} 02:00")
        store.get_nwp("icon", as_of, horizon(as_of))
    assert len(calls) == 1
