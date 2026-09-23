"""Загрузчик Previous Runs без сети: разбор ответа, кэш, повторы и ошибки."""

import hashlib
from datetime import date
from urllib.parse import parse_qs

import httpx
import pandas as pd
import pytest

from src.forecast.weather import fetch_prev_runs as fpr
from src.forecast.weather.asof import AsOfStore
from src.forecast.weather.prev_runs_rule import PREV_DAYS, choose_n, run_init_for
from src.forecast.weather.sources import SOURCES

GEM = fpr.MODELS["gem"]


def payload(model: fpr.PrevRunsModel, times: list[str], value=lambda var, n, i: 5.0 + i + n / 10) -> dict:
    """Ответ API в том виде, в каком его отдает Open-Meteo: списки по переменным."""
    hourly = {"time": times}
    for i, _ in enumerate(times):
        for var in model.variables:
            for n in PREV_DAYS:
                hourly.setdefault(f"{var}_previous_day{n}", []).append(value(var, n, i))
        for var in model.qc_variables:
            hourly.setdefault(var, []).append(value(var, 0, i))
    return {"utc_offset_seconds": 0, "hourly": hourly}


def test_to_long_small_example():
    times = ["2026-02-10T11:00", "2026-02-10T12:00", "2026-02-10T13:00"]
    data = payload(GEM, times)
    # В первый час у previous_day3 нет ветра ни на одной высоте: такой строки быть не должно.
    for var in GEM.wind_speeds:
        data["hourly"][f"{var}_previous_day3"][0] = None

    frame = fpr.to_long(data, GEM)

    assert list(frame.columns) == list(fpr.COLUMNS)
    assert len(frame) == 3 * 3 - 1
    row = frame[(frame["valid_time_utc"] == pd.Timestamp("2026-02-10 12:00", tz="UTC")) & (frame["prev_day"] == 1)].iloc[0]
    assert row["run_init_utc"] == pd.Timestamp("2026-02-09 12:00", tz="UTC")
    assert row["wind_speed_80m"] == pytest.approx(6.1)
    # 11:00 у GEM интерполирован между 09:00 и 12:00 из следующего прогона: метка — прогон 12z.
    early = frame[(frame["valid_time_utc"] == pd.Timestamp("2026-02-10 11:00", tz="UTC")) & (frame["prev_day"] == 2)].iloc[0]
    assert early["run_init_utc"] == pd.Timestamp("2026-02-08 12:00", tz="UTC")
    assert frame["wind_speed_100m"].isna().all()
    assert frame["wind_direction_100m"].isna().all()
    assert not frame.duplicated(["run_init_utc", "valid_time_utc"]).any()
    assert frame["run_init_utc"].is_monotonic_increasing


def test_to_long_rejects_local_time():
    data = payload(GEM, ["2026-02-10T11:00"])
    data["utc_offset_seconds"] = 21600
    with pytest.raises(fpr.FetchError):
        fpr.to_long(data, GEM)


def test_request_variables_only_what_model_has():
    variables = fpr.request_variables(GEM)
    assert "wind_speed_100m_previous_day1" not in variables
    assert "wind_direction_80m_previous_day3" in variables
    assert len(variables) == len(GEM.variables) * 3 + 3


def test_qc_counts_separates_rounding_from_copy():
    times = ["2026-02-10T00:00", "2026-02-10T01:00", "2026-02-10T02:00"]
    data = payload(GEM, times)
    hourly = data["hourly"]
    wind = GEM.wind_speeds[0]
    # Час 0: совпал только ветер, как бывает из-за округления.
    hourly[f"{wind}_previous_day1"][0] = hourly[wind][0]
    # Час 1: previous_day1 целиком копия свежего прогона, и все три дня одинаковы.
    for var in GEM.qc_variables:
        for n in PREV_DAYS:
            hourly[f"{var}_previous_day{n}"][1] = hourly[var][1]

    qc = fpr.qc_counts(data, GEM)

    assert qc["hours"] == 3
    assert qc["day1_same_wind"] == 2
    assert qc["day1_same_all"] == 1
    assert qc["all_days_same_all"] == 1


def test_write_cache_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    frame = fpr.to_long(payload(GEM, ["2025-12-31T23:00", "2026-01-01T00:00"]), GEM)

    first = [p.read_bytes() for p in fpr.write_cache(frame, "gem")]
    second = [p.read_bytes() for p in fpr.write_cache(frame.sample(frac=1, random_state=0), "gem")]

    assert first == second
    assert sorted(fpr.read_checksums("gem")) == ["2025.csv.gz", "2026.csv.gz"]
    back = fpr.read_cache("gem")
    pd.testing.assert_frame_equal(back, frame, check_dtype=False)


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


OK = {"hourly": {"time": []}}
# Так Open-Meteo отвечает, когда сбой случился посреди потока: статус 200, тело не JSON.
STREAM_ERROR = b"Unexpected error while streaming data: modelRunUnavailable(model: App.DomainRegistry.cmc_gem_gdps_15km)"


def test_get_json_retries_server_errors_and_network():
    answers = iter([httpx.Response(503), "network", httpx.Response(200, json=OK)])
    sleeps = []

    def handler(request):
        answer = next(answers)
        if answer == "network":
            raise httpx.ConnectError("down", request=request)
        return answer

    with mock_client(handler) as client:
        assert fpr.get_json(client, {}, sleep=sleeps.append) == OK
    assert sleeps == [fpr.BACKOFF_S, fpr.BACKOFF_S * 2]


def test_get_json_retries_stream_error_with_status_200():
    answers = iter([httpx.Response(200, content=STREAM_ERROR), httpx.Response(200, json=OK)])
    sleeps = []
    with mock_client(lambda request: next(answers)) as client:
        assert fpr.get_json(client, {}, sleep=sleeps.append) == OK
    assert sleeps == [fpr.BACKOFF_S]


@pytest.mark.parametrize(
    "answer", [httpx.Response(200, content=STREAM_ERROR), httpx.Response(200, json={"latitude": 43.6})], ids=["not_json", "no_hourly"]
)
def test_get_json_bad_200_ends_in_fetch_error(answer):
    with mock_client(lambda request: answer) as client, pytest.raises(fpr.FetchError, match="hourly"):
        fpr.get_json(client, {}, sleep=lambda s: None)


def test_get_json_waits_minute_on_rate_limit():
    answers = iter([httpx.Response(429), httpx.Response(200, json=OK)])
    sleeps = []
    with mock_client(lambda request: next(answers)) as client:
        fpr.get_json(client, {}, sleep=sleeps.append)
    assert sleeps == [fpr.RATE_LIMIT_PAUSE_S]


def test_get_json_client_error_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": True, "reason": "Cannot initialize WeatherVariable"})

    with mock_client(handler) as client, pytest.raises(fpr.FetchError, match="400"):
        fpr.get_json(client, {}, sleep=lambda s: None)
    assert len(calls) == 1


def test_get_json_gives_up_after_attempts():
    with mock_client(lambda request: httpx.Response(500)) as client, pytest.raises(fpr.FetchError, match="4 попыток"):
        fpr.get_json(client, {}, sleep=lambda s: None)


def test_fetch_model_one_request_per_year():
    requests = []

    def handler(request):
        params = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        requests.append(params)
        hours = pd.date_range(params["start_date"], f"{params['end_date']} 23:00", freq="h")
        return httpx.Response(200, json=payload(GEM, [f"{t:%Y-%m-%dT%H:%M}" for t in hours]))

    with mock_client(handler) as client:
        frame, qc = fpr.fetch_model(GEM, client, start=date(2025, 12, 31), end=date(2026, 1, 1), sleep=lambda s: None)

    assert [(r["start_date"], r["end_date"]) for r in requests] == [("2025-12-31", "2025-12-31"), ("2026-01-01", "2026-01-01")]
    assert {r["models"] for r in requests} == {"gem_global"}
    assert requests[0]["timezone"] == "GMT"
    assert len(frame) == 48 * 3
    assert qc["hours"] == 48


def test_fetch_model_fails_on_empty_variable():
    def handler(request):
        data = payload(GEM, ["2026-01-01T00:00"])
        for n in PREV_DAYS:
            data["hourly"][f"relative_humidity_2m_previous_day{n}"] = [None]
        return httpx.Response(200, json=data)

    with mock_client(handler) as client, pytest.raises(fpr.FetchError, match="relative_humidity_2m"):
        fpr.fetch_model(GEM, client, start=date(2026, 1, 1), end=date(2026, 1, 1), sleep=lambda s: None)


@pytest.mark.parametrize("source", sorted(fpr.MODELS))
def test_committed_cache(source):
    """Закоммиченный кэш: хэши сходятся, пары уникальны, прогон соответствует правилу."""
    model = fpr.MODELS[source]
    expected = fpr.read_checksums(source)
    files = sorted(p.name for p in (fpr.nwp_dir() / source).glob("*.csv.gz"))
    assert files == sorted(expected) == ["2024.csv.gz", "2025.csv.gz", "2026.csv.gz"]
    for name, digest in expected.items():
        assert hashlib.sha256((fpr.nwp_dir() / source / name).read_bytes()).hexdigest() == digest

    frame = fpr.read_cache(source)

    assert list(frame.columns) == list(fpr.COLUMNS)
    assert not frame.duplicated(["run_init_utc", "valid_time_utc"]).any()
    assert set(frame["prev_day"]) == set(PREV_DAYS)
    for n in PREV_DAYS:
        part = frame[frame["prev_day"] == n]
        expected_init = run_init_for(pd.DatetimeIndex(part["valid_time_utc"]), n, model.cycle_h, model.data_step_h)
        assert (pd.DatetimeIndex(part["run_init_utc"]) == expected_init).all()
        # После начала архива каждый previous_dayN идет каждый час, без дыр.
        valid = pd.DatetimeIndex(part["valid_time_utc"]).sort_values()
        assert valid.equals(pd.date_range(valid[0], valid[-1], freq="h"))
    assert frame["valid_time_utc"].min() >= pd.Timestamp(fpr.START, tz="UTC")
    assert frame["valid_time_utc"].max() == pd.Timestamp(fpr.END, tz="UTC") + pd.Timedelta(hours=23)
    absent = [v for v in fpr.VARIABLES if v not in model.variables]
    assert frame[absent].isna().all().all()
    # Весь горизонт ретро-симуляции, февраль и +48 ч последнего выпуска, без пропусков ветра.
    feb = frame[frame["valid_time_utc"] >= pd.Timestamp("2026-02-01", tz="UTC")]
    assert feb["valid_time_utc"].max() >= pd.Timestamp("2026-03-01 02:00", tz="UTC")
    assert (feb.groupby("prev_day").size() == 29 * 24).all()
    assert feb[model.wind_speeds[0]].notna().all()


@pytest.mark.parametrize("source", sorted(fpr.MODELS))
def test_asof_store_on_committed_cache_matches_rule(source):
    """На всех 28 выпусках ретро-симуляции AsOfStore берет тот прогон, что дает choose_n."""
    model, registered = fpr.MODELS[source], SOURCES[source]
    assert registered.run_step_h == model.cycle_h
    store = AsOfStore(fpr.nwp_dir())
    for issue in pd.date_range("2026-01-31 02:00", "2026-02-27 02:00", freq="D", tz="UTC"):
        valid = pd.date_range(issue + pd.Timedelta(hours=1), periods=48, freq="h")
        out = store.get_nwp(source, issue, valid)
        step = model.data_step_h
        expected = [run_init_for(t, choose_n(t, issue, model.cycle_h, registered.delay, step), model.cycle_h, step) for t in valid]
        assert list(out["valid_time_utc"]) == list(valid)
        assert list(out["run_init_utc"]) == expected
        assert (out["available_at_utc"] <= issue).all()


@pytest.mark.parametrize("source", sorted(s for s, m in fpr.MODELS.items() if m.data_step_h > 1))
def test_committed_cache_hour_labeled_with_newest_run_of_interpolation_window(source):
    """У моделей с 3-часовыми данными метка часа не старше прогона любой точки, по которой он интерполирован.

    Open-Meteo строит промежуточный час по точкам floor_3h(t) − 3 ч … floor_3h(t) + 6 ч
    склеенной серии previous_dayN, и последняя точка бывает из следующего прогона.
    Метка старше нее означала бы, что AsOfStore отдаст час до публикации этого прогона.
    """
    step = pd.Timedelta(hours=fpr.MODELS[source].data_step_h)
    frame = fpr.read_cache(source)
    labels = frame.set_index(["valid_time_utc", "prev_day"])["run_init_utc"]
    hours = frame[frame["valid_time_utc"].dt.hour % step.components.hours != 0]
    base = hours["valid_time_utc"].dt.floor(step)
    checked = 0
    for k in (-1, 0, 1, 2):
        point = pd.MultiIndex.from_arrays([base + k * step, hours["prev_day"]])
        point_label = labels.reindex(point).to_numpy()
        present = ~pd.isna(point_label)
        assert (hours["run_init_utc"].to_numpy()[present] >= point_label[present]).all()
        checked += int(present.sum())
    assert checked > 3 * len(hours)
