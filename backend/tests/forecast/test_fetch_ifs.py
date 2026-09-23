"""Загрузчик IFS и его кэш. Без сети: API подменяется через httpx.MockTransport."""

import gzip
from datetime import UTC, date, datetime, timedelta

import httpx
import pandas as pd
import pytest

from src.forecast.weather import fetch_ifs as ifs
from src.forecast.weather.asof import AsOfStore, NoRunAvailable

RUN = datetime(2024, 10, 1, 12, tzinfo=UTC)


def make_payload(run: datetime, hours: int = ifs.HOURS) -> dict:
    times = [(run + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(hours)]
    hourly: dict[str, list] = {"time": times}
    for i, name in enumerate(ifs.VARIABLES):
        hourly[name] = [round(i + h / 10, 2) for h in range(hours)]
    hourly["wind_gusts_10m"][0] = None
    return {"utc_offset_seconds": 0, "timezone": "GMT", "hourly": hourly}


def unavailable(run: str) -> httpx.Response:
    return httpx.Response(400, json={"error": True, "reason": f"The requested model run is not available. Model: ecmwf_ifs, run: {run}Z"})


class Api:
    """Поддельный Single Runs: отвечает по очереди из ``script``, потом нормальным прогоном."""

    def __init__(self, script=None, missing: set[str] = frozenset()):
        self.script = list(script or [])
        self.missing = missing
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        run = request.url.params["run"]
        self.calls.append(run)
        if self.script:
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            return step
        if run in self.missing:
            return unavailable(run)
        return httpx.Response(200, json=make_payload(datetime.fromisoformat(run).replace(tzinfo=UTC)))

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


def no_wait() -> ifs.RateLimiter:
    return ifs.RateLimiter(0, sleep=lambda _: None)


# ---------------------------------------------------------------- план прогонов


def test_planned_runs_take_12z_18z_and_all_runs_in_february():
    runs = ifs.planned_runs(date(2026, 1, 31), date(2026, 2, 1))
    assert [run.strftime("%d %H") for run in runs] == ["31 12", "31 18", "01 00", "01 06", "01 12", "01 18"]


def test_full_plan_fits_daily_limit():
    runs = ifs.planned_runs(ifs.ARCHIVE_START, ifs.ARCHIVE_END)
    assert len(runs) == len(set(runs)) == 688 * 2 + 28 * 4


# ---------------------------------------------------------------- разбор ответа


def test_parse_run_gives_72_utc_hours_per_run():
    frame = ifs.parse_run(make_payload(RUN), RUN)
    assert list(frame.columns) == list(ifs.COLUMNS)
    assert len(frame) == ifs.HOURS
    assert (frame["run_init_utc"] == pd.Timestamp(RUN)).all()
    assert frame["valid_time_utc"].iloc[0] == pd.Timestamp(RUN)
    assert frame["valid_time_utc"].iloc[-1] == pd.Timestamp(RUN + timedelta(hours=71))
    assert str(frame["valid_time_utc"].dt.tz) == "UTC"
    assert pd.isna(frame["wind_gusts_10m"].iloc[0])


def test_parse_run_keeps_values_in_their_columns():
    payload = make_payload(RUN)
    frame = ifs.parse_run(payload, RUN)
    for name in ifs.VARIABLES:
        expected = [float("nan") if value is None else value for value in payload["hourly"][name]]
        assert frame[name].tolist() == pytest.approx(expected, nan_ok=True), name


def test_parse_run_fills_missing_hours_with_nan_and_reports_them():
    frame = ifs.parse_run(make_payload(RUN, hours=70), RUN)
    assert len(frame) == ifs.HOURS
    gaps = ifs.find_gaps(frame)
    assert {row["variable"] for row in gaps} == set(ifs.VARIABLES)
    assert all(row["lead_hours"] == "70;71" for row in gaps)


def test_find_gaps_ignores_gust_at_lead_zero_only():
    payload = make_payload(RUN)
    payload["hourly"]["temperature_2m"][5] = None
    gaps = ifs.find_gaps(ifs.parse_run(payload, RUN))
    assert gaps == [{"run_init_utc": "2024-10-01T12:00:00Z", "variable": "temperature_2m", "lead_hours": "5", "reason": "null"}]


def test_parse_run_rejects_missing_variable():
    payload = make_payload(RUN)
    del payload["hourly"]["wind_speed_80m"]
    with pytest.raises(ifs.FetchError, match="wind_speed_80m"):
        ifs.parse_run(payload, RUN)


def test_parse_run_rejects_non_utc_response():
    payload = make_payload(RUN) | {"utc_offset_seconds": 21600}
    with pytest.raises(ifs.FetchError, match="UTC"):
        ifs.parse_run(payload, RUN)


# ---------------------------------------------------------------- повторы и лимиты


def test_request_run_sends_exact_run_and_units():
    api = Api()
    with api.client() as client:
        ifs.request_run(client, RUN, no_wait())
    assert api.calls == ["2024-10-01T12:00"]


def test_request_run_retries_server_errors_and_network_failures():
    api = Api([httpx.Response(502, text="bad gateway"), httpx.ConnectError("refused")])
    pauses: list[float] = []
    with api.client() as client:
        payload = ifs.request_run(client, RUN, no_wait(), sleep=pauses.append)
    assert len(payload["hourly"]["time"]) == ifs.HOURS
    assert len(api.calls) == 3
    assert pauses == [ifs.BACKOFF_S, ifs.BACKOFF_S * 2]


def test_request_run_waits_out_minutely_limit():
    api = Api([httpx.Response(429, json={"error": True, "reason": "Minutely API request limit exceeded"})])
    pauses: list[float] = []
    with api.client() as client:
        ifs.request_run(client, RUN, no_wait(), sleep=pauses.append)
    assert pauses == [ifs.RATE_LIMIT_PAUSE_S]


def test_request_run_stops_on_daily_limit():
    api = Api([httpx.Response(429, json={"error": True, "reason": "Daily API request limit exceeded"})])
    with api.client() as client, pytest.raises(ifs.FetchError, match="дневной лимит"):
        ifs.request_run(client, RUN, no_wait(), sleep=lambda _: None)
    assert len(api.calls) == 1


def test_request_run_gives_up_after_retries():
    api = Api([httpx.Response(503, text="down")] * 3)
    with api.client() as client, pytest.raises(ifs.FetchError, match="после 3 попыток"):
        ifs.request_run(client, RUN, no_wait(), retries=3, sleep=lambda _: None)
    assert len(api.calls) == 3


def test_request_run_does_not_retry_unavailable_run():
    api = Api([unavailable("2024-10-01T12:00")])
    with api.client() as client, pytest.raises(ifs.RunUnavailable):
        ifs.request_run(client, RUN, no_wait())
    assert len(api.calls) == 1


def test_request_run_does_not_retry_bad_request():
    api = Api([httpx.Response(400, json={"error": True, "reason": "Cannot initialize WeatherVariable from invalid String value"})])
    with api.client() as client, pytest.raises(ifs.FetchError, match="invalid String") as exc:
        ifs.request_run(client, RUN, no_wait())
    assert not isinstance(exc.value, ifs.RunUnavailable)
    assert len(api.calls) == 1


def test_rate_limiter_keeps_min_interval():
    now = [0.0]
    pauses: list[float] = []

    def sleep(seconds: float) -> None:
        pauses.append(seconds)
        now[0] += seconds

    limiter = ifs.RateLimiter(1.0, clock=lambda: now[0], sleep=sleep)
    limiter.wait()
    now[0] += 0.25
    limiter.wait()
    now[0] += 3.0
    limiter.wait()
    assert pauses == [0.75]


# ---------------------------------------------------------------- загрузка в кэш


def test_fetch_skips_unavailable_runs_and_resumes_without_requests(tmp_path):
    api = Api(missing={"2024-10-01T18:00"})
    with api.client() as client:
        summary = ifs.fetch(date(2024, 9, 30), date(2024, 10, 1), tmp_path, client, no_wait())
    assert (summary.planned, summary.downloaded, summary.unavailable) == (4, 3, 1)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["2024-09.csv.gz", "2024-10.csv.gz", "SHA256SUMS", "gaps.csv"]
    gaps = ifs.read_gaps(tmp_path)
    assert gaps.to_dict("records") == [{"run_init_utc": "2024-10-01T18:00:00Z", "variable": "*", "lead_hours": "0-71", "reason": ifs.UNAVAILABLE}]
    assert ifs.verify_sums(tmp_path) == []

    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    with api.client() as client:
        again = ifs.fetch(date(2024, 9, 30), date(2024, 10, 1), tmp_path, client, no_wait())
    assert len(api.calls) == 4
    assert (again.already_cached, again.downloaded) == (4, 0)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == before


def test_fetch_retry_unavailable_replaces_gap_record(tmp_path):
    with Api(missing={"2024-10-01T18:00"}).client() as client:
        ifs.fetch(date(2024, 10, 1), date(2024, 10, 1), tmp_path, client, no_wait())
    api = Api()
    with api.client() as client:
        summary = ifs.fetch(date(2024, 10, 1), date(2024, 10, 1), tmp_path, client, no_wait(), retry_unavailable=True)
    assert api.calls == ["2024-10-01T18:00"]
    assert summary.downloaded == 1
    assert ifs.read_gaps(tmp_path).empty
    assert len(ifs.load_runs(tmp_path)) == 2 * ifs.HOURS
    assert ifs.verify_sums(tmp_path) == []


def damaged_run_response() -> httpx.Response:
    """Прогон RUN, у которого ветер на 80 м пуст на часах 3 и 4, как у поврежденных прогонов архива."""
    payload = make_payload(RUN)
    payload["hourly"]["wind_speed_80m"][3:5] = [None, None]
    return httpx.Response(200, json=payload)


def test_fetch_lists_null_values_in_gaps(tmp_path):
    with Api([damaged_run_response()]).client() as client:
        ifs.fetch(date(2024, 10, 1), date(2024, 10, 1), tmp_path, client, no_wait())
    assert ifs.read_gaps(tmp_path).to_dict("records") == [
        {"run_init_utc": "2024-10-01T12:00:00Z", "variable": "wind_speed_80m", "lead_hours": "3;4", "reason": "null"}
    ]
    assert ifs.verify_sums(tmp_path) == []


def test_fetch_saves_progress_when_stopped(tmp_path):
    api = Api([httpx.Response(200, json=make_payload(datetime(2024, 10, 1, 12, tzinfo=UTC)))] + [httpx.Response(503, text="down")] * 5)
    with api.client() as client, pytest.raises(ifs.FetchError):
        ifs.fetch(date(2024, 10, 1), date(2024, 10, 1), tmp_path, client, no_wait(), sleep=lambda _: None)
    assert ifs.cached_runs(tmp_path) == {datetime(2024, 10, 1, 12, tzinfo=UTC)}
    assert ifs.verify_sums(tmp_path) == []


def test_serialize_month_is_byte_stable():
    frame = ifs.parse_run(make_payload(RUN), RUN)
    shuffled = frame.sample(frac=1, random_state=0)
    assert ifs.serialize_month(frame) == ifs.serialize_month(shuffled)


def test_verify_sums_catches_changed_file(tmp_path):
    with Api().client() as client:
        ifs.fetch(date(2024, 10, 1), date(2024, 10, 1), tmp_path, client, no_wait())
    path = tmp_path / "2024-10.csv.gz"
    path.write_bytes(path.read_bytes() + b"\0")
    assert ifs.verify_sums(tmp_path) == ["2024-10.csv.gz: хэш не совпадает"]


# ---------------------------------------------------------------- закоммиченный кэш

CACHE = ifs.default_cache_dir()


@pytest.fixture(scope="module")
def cache() -> pd.DataFrame:
    return ifs.load_runs(CACHE)


def test_committed_cache_matches_sha256sums():
    assert ifs.verify_sums(CACHE) == []


def test_committed_cache_covers_whole_plan(cache):
    cached = set(cache["run_init_utc"].dt.to_pydatetime())
    missing = ifs.unavailable_runs(CACHE)
    planned = set(ifs.planned_runs(ifs.ARCHIVE_START, ifs.ARCHIVE_END))
    assert planned - cached - missing == set()
    assert cached.isdisjoint(missing)


def test_committed_cache_has_all_hours_of_every_run(cache):
    leads = (cache["valid_time_utc"] - cache["run_init_utc"]) // pd.Timedelta(hours=1)
    per_run = leads.groupby(cache["run_init_utc"]).agg(list)
    assert all(hours == list(range(ifs.HOURS)) for hours in per_run)


def test_committed_cache_nans_are_listed_in_gaps(cache):
    listed = {(row.run_init_utc, row.variable, row.lead_hours) for row in ifs.read_gaps(CACHE).itertuples()}
    found = {(row["run_init_utc"], row["variable"], row["lead_hours"]) for row in ifs.find_gaps(cache)}
    assert found - listed == set()


def test_committed_cache_rewrites_to_same_content():
    for path in ifs.month_files(CACHE):
        assert gzip.decompress(ifs.serialize_month(ifs.read_month(path))) == gzip.decompress(path.read_bytes()), path.name


def test_asof_store_serves_every_issue_of_backtest_and_replay():
    """Январь 2026 (бэктест) и февраль (симуляция): выпуск в 02:00 UTC видит 18z предыдущего дня на все 48 ч."""
    store = AsOfStore(CACHE.parent)
    for issue in pd.date_range("2026-01-01T02:00Z", "2026-02-27T02:00Z", freq="D"):
        nwp = store.get_nwp("ifs", issue, pd.date_range(issue + pd.Timedelta(hours=1), periods=48, freq="h"))
        assert (nwp["run_init_utc"] == issue - pd.Timedelta(hours=8)).all(), issue
        assert nwp[["ws80", "wd100", "t2m"]].notna().all().all(), issue


def test_asof_store_never_serves_hours_without_wind():
    """Вся история выпусков: ветер на всех 48 ч или явный NoRunAvailable там, где архив поврежден (08.2025)."""
    store = AsOfStore(CACHE.parent)
    refused = []
    for issue in pd.date_range("2024-03-16T02:00Z", "2026-02-27T02:00Z", freq="D"):
        try:
            nwp = store.get_nwp("ifs", issue, pd.date_range(issue + pd.Timedelta(hours=1), periods=48, freq="h"))
        except NoRunAvailable:
            refused.append(f"{issue:%Y-%m-%d}")
            continue
        assert nwp["ws80"].notna().all(), issue
    assert refused == ["2025-08-05", "2025-08-06", "2025-08-08", "2025-08-09"]
