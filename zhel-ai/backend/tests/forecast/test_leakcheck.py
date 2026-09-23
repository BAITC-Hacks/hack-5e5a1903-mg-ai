"""make leakcheck: ловит подложенные утечки и проходит на реальном кэше."""

import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from src.forecast import leakcheck
from src.forecast.dataset import config as dataset_config
from src.forecast.weather import manifest as manifest_module
from src.forecast.weather.asof import AsOfStore, LeakageError
from src.forecast.weather.fetch_prev_runs import MODELS
from src.forecast.weather.prev_runs_rule import run_init_for
from src.forecast.weather.sources import SOURCES

DATA_DIR = dataset_config.DATA_DIR
CACHE = DATA_DIR / "nwp"
ISSUE = pd.Timestamp("2026-02-10 02:00", tz="UTC")


def ts(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def rows(source: str = "gfs", init: str = "2026-02-09 18:00") -> pd.DataFrame:
    """48 часов выпуска ``ISSUE`` из одного прогона, опубликованного до выпуска."""
    run = ts(init)
    valid = leakcheck.horizon(ISSUE)
    return pd.DataFrame(
        {
            "valid_time_utc": valid,
            "source": source,
            "run_init_utc": run,
            "available_at_utc": run + SOURCES[source].delay,
            "lead_h": ((valid - run) / pd.Timedelta(hours=1)).astype(int),
            "ws100": 7.0,
        }
    )


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class StubStore:
    """Хранилище, которое отдает заданную таблицу как есть, без собственной проверки."""

    def __init__(self, frame: pd.DataFrame | None = None, error: Exception | None = None):
        self.frame, self.error = frame, error

    def get_nwp_multi(self, sources, as_of, valid_times):
        if self.error is not None:
            raise self.error
        return self.frame


@pytest.fixture(scope="module")
def february():
    return leakcheck.check_issues(AsOfStore(CACHE), leakcheck.issue_times(*leakcheck.FEB_ISSUES), keep_frames=True)


# Строки погоды выпуска


def test_rows_of_published_run_are_clean():
    assert leakcheck.check_rows(rows(), ISSUE) == []


def test_row_from_the_future_is_caught():
    frame = rows()
    # GFS 00z дня выпуска выходит в 07:00, через пять часов после выпуска.
    frame.loc[5, ["run_init_utc", "available_at_utc"]] = [ts("2026-02-10 00:00"), ts("2026-02-10 07:00")]

    problems = leakcheck.check_rows(frame, ISSUE)

    assert any("опубликованы позже выпуска" in problem for problem in problems)
    assert any("выходит позже выпуска" in problem for problem in problems)


def test_available_at_column_is_not_trusted():
    frame = rows()
    # Колонка утверждает, что прогон 00z вышел в 01:00, а по задержке GFS он выходит в 07:00.
    frame.loc[5, ["run_init_utc", "available_at_utc"]] = [ts("2026-02-10 00:00"), ts("2026-02-10 01:00")]

    problems = leakcheck.check_rows(frame, ISSUE)

    assert any("раньше, чем прогон мог выйти" in problem for problem in problems)
    assert any("выходит позже выпуска" in problem for problem in problems)


def test_unknown_source_and_rows_outside_horizon_are_caught():
    frame = rows()
    frame.loc[0, "source"] = "era5"
    frame.loc[1, "valid_time_utc"] = ISSUE

    problems = leakcheck.check_rows(frame, ISSUE)

    assert any("неизвестного источника" in problem for problem in problems)
    assert any("вне горизонта" in problem for problem in problems)


def test_check_issues_catches_future_row_the_store_let_through():
    frame = rows()
    frame.loc[0, ["run_init_utc", "available_at_utc"]] = [ts("2026-02-10 00:00"), ts("2026-02-10 07:00")]

    result = leakcheck.check_issues(StubStore(frame), [ISSUE])

    assert result.with_weather == 1
    assert result.problems


def test_check_issues_reports_leakage_error_of_the_store():
    result = leakcheck.check_issues(StubStore(error=LeakageError("прогон из будущего")), [ISSUE])

    assert result.with_weather == 0
    assert "прогон из будущего" in result.problems[0]


def test_february_on_real_cache_is_clean(february):
    assert february.issues == 28
    assert february.with_weather == 28
    assert february.problems == []
    assert set(february.rows_by_source) == set(SOURCES)


# Паспорта


def test_manifests_are_deterministic_and_not_after_issue(february, tmp_path):
    frames = dict(sorted(february.frames.items())[:2])
    stale = tmp_path / "a" / "stale.json"
    stale.parent.mkdir()
    stale.write_text("{}", encoding="utf-8")

    first, problems = leakcheck.write_manifests(frames, tmp_path / "a", DATA_DIR)
    second, _ = leakcheck.write_manifests(frames, tmp_path / "b", DATA_DIR)

    assert problems == []
    assert first == second
    assert not stale.exists()
    for issue in frames:
        name = f"{issue:%Y-%m-%d}.json"
        content = (tmp_path / "a" / name).read_bytes()
        assert content == (tmp_path / "b" / name).read_bytes()
        manifest = json.loads(content)
        assert pd.Timestamp(manifest["max_available_at_utc"]) <= issue
        assert manifest["git_sha"] is None


def test_manifest_of_leaking_issue_is_not_written(tmp_path):
    frame = rows()
    frame.loc[0, ["run_init_utc", "available_at_utc"]] = [ts("2026-02-10 00:00"), ts("2026-02-10 07:00")]

    written, problems = leakcheck.write_manifests({ISSUE: frame}, tmp_path, DATA_DIR)

    assert written == []
    assert "паспорт не собран" in problems[0]
    assert list(tmp_path.glob("*.json")) == []


# Previous Runs


def prev_runs_cache(root: Path, source: str, labels: pd.DatetimeIndex, valid: pd.DatetimeIndex) -> None:
    folder = root / SOURCES[source].cache_dir
    folder.mkdir(parents=True)
    frame = pd.DataFrame(
        {"run_init_utc": labels.strftime("%Y-%m-%dT%H:%M:%SZ"), "valid_time_utc": valid.strftime("%Y-%m-%dT%H:%M:%SZ"), "prev_day": 1}
    )
    frame.to_csv(folder / "2026.csv.gz", index=False)


def test_prev_runs_label_older_than_rule_is_a_leak(tmp_path):
    # Ошибка #60: у IFS 0.25° час 04 интерполирован и с точкой 09 из прогона 06z,
    # а метка floor_6h(t) − 24 ч говорит о прогоне 00z и занижает время публикации.
    model = MODELS["ifs025"]
    valid = pd.date_range("2026-02-10 00:00", periods=24, freq="h", tz="UTC")
    labels = run_init_for(valid, 1, model.cycle_h, model.data_step_h)
    naive = valid.floor("6h") - pd.Timedelta(hours=24)
    prev_runs_cache(tmp_path, "ifs025", naive, valid)

    table, problems, _ = leakcheck.check_prev_runs_labels(tmp_path, {"ifs025": model})

    assert (labels != naive).sum() == table[0]["older"] > 0
    assert "старше правила" in problems[0]


def test_prev_runs_label_newer_than_rule_is_only_a_note(tmp_path):
    model = MODELS["gfs"]
    valid = pd.date_range("2026-02-10 00:00", periods=24, freq="h", tz="UTC")
    labels = run_init_for(valid, 1, model.cycle_h, model.data_step_h) + pd.Timedelta(hours=6)
    prev_runs_cache(tmp_path, "gfs", labels, valid)

    _, problems, notes = leakcheck.check_prev_runs_labels(tmp_path, {"gfs": model})

    assert problems == []
    assert "новее правила" in notes[0]


def test_prev_runs_labels_of_real_cache_match_the_rule():
    table, problems, _ = leakcheck.check_prev_runs_labels(CACHE)

    assert problems == []
    assert {row["source"] for row in table} == set(MODELS)


def test_boundary_hours_never_pick_a_run_newer_than_issue(february):
    issues = leakcheck.issue_times(*leakcheck.FEB_ISSUES)

    table, problems, checked = leakcheck.check_prev_runs_boundaries(issues, [], february.frames)

    assert problems == []
    assert checked == len(issues) * leakcheck.HORIZON_H * len(MODELS)
    assert {(row["source"], row["lead"]) for row in table} == {(name, lead) for name in MODELS for lead in leakcheck.BOUNDARY_LEADS}
    # previous_day1 вслепую на +25 и +48 всегда брал бы прогон, вышедший после выпуска.
    assert all(row["naive"] == f"{len(issues)} из {len(issues)}" for row in table if row["lead"] in (25, 48))


def test_store_choice_newer_than_rule_is_caught():
    valid = ISSUE + pd.Timedelta(hours=25)
    frame = pd.DataFrame({"source": ["gfs"], "valid_time_utc": [valid], "run_init_utc": [ts("2026-02-10 00:00")]})

    _, problems, _ = leakcheck.check_prev_runs_boundaries([ISSUE], [], {ISSUE: frame}, {"gfs": MODELS["gfs"]})

    assert any("AsOfStore взял прогон" in problem for problem in problems)


# Запрещенные API


def test_archive_api_url_in_code_is_caught(tmp_path):
    write(tmp_path, {"backend/src/forecast/weather/era.py": 'URL = "https://archive-api.open-meteo.com/v1/archive"\n'})

    section = leakcheck.scan_forbidden_apis(tmp_path, roots=("backend/src",), files=())

    assert not section.ok
    assert "backend/src/forecast/weather/era.py:1" in section.problems[0]


@pytest.mark.parametrize(
    "code",
    [
        'params = {"models": "era5_seamless"}\n',
        'URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"\n',
        "def fetch_era5():\n    return None\n",
    ],
)
def test_reanalysis_and_historical_forecast_are_caught(tmp_path, code):
    write(tmp_path, {"ml/training/extra.py": code})

    assert not leakcheck.scan_forbidden_apis(tmp_path, roots=("ml/training",), files=()).ok


def test_archive_api_in_compose_is_caught(tmp_path):
    write(tmp_path, {"docker-compose.yml": "services:\n  x:\n    environment:\n      URL: https://archive-api.open-meteo.com\n"})

    section = leakcheck.scan_forbidden_apis(tmp_path, roots=(), files=("docker-compose.yml",))

    assert "docker-compose.yml:4" in section.problems[0]


def test_docstrings_and_comments_may_explain_the_ban(tmp_path):
    code = '"""ERA5 и archive-api запрещены: это факт, а не прогноз."""\n\n# ERA5 не используем.\nX = 1\n'
    write(tmp_path, {"backend/src/forecast/doc.py": code})

    assert leakcheck.scan_forbidden_apis(tmp_path, roots=("backend/src",), files=()).ok


def test_diagnostics_are_allowed_but_must_not_be_imported(tmp_path):
    write(
        tmp_path,
        {
            "backend/src/analysis/diag.py": 'URL = "https://archive-api.open-meteo.com/v1/archive"\n',
            "backend/src/forecast/uses.py": "from src.analysis import diag\n",
        },
    )

    section = leakcheck.scan_forbidden_apis(tmp_path, roots=("backend/src",), files=())

    assert len(section.problems) == 1
    assert "импортирует `src.analysis.diag`" in section.problems[0]
    assert any("backend/src/analysis/diag.py" in detail for detail in section.details)


def test_real_code_does_not_call_forbidden_apis():
    section = leakcheck.scan_forbidden_apis()

    assert section.problems == []
    assert any("backend/src/analysis/" in detail for detail in section.details)


# Признаки модели


def ml_repo(root: Path, features: str, extra: dict[str, str] | None = None) -> None:
    write(root, {"ml/src/ml_service/features.py": features, "ml/src/ml_service/frame.py": "HUB = 80\n", **(extra or {})})


def test_scada_feature_is_caught(tmp_path):
    features = 'FEATURES = ["ws", "t", "power_lag1"]\n\n\ndef build(scada):\n    return scada["power_norm"].shift(1)\n'
    ml_repo(tmp_path, features)

    section = leakcheck.check_features(tmp_path)

    assert not section.ok
    text = "\n".join(section.problems)
    assert "power_lag1" in text
    assert "`power_norm`" in text
    assert ".shift(...)" in text


def test_scada_loader_in_model_service_is_caught(tmp_path):
    ml_repo(tmp_path, 'FEATURES = ["ws", "t"]\n', {"ml/src/ml_service/service.py": "from src.forecast.dataset.scada import load_scada\n"})

    section = leakcheck.check_features(tmp_path)

    assert any("загрузчик SCADA" in problem for problem in section.problems)


def test_training_on_measured_scada_is_a_note_not_a_leak(tmp_path):
    training = 'def fit(g):\n    return build_features(g["wind_ms"], g["temp_c"], g["time_utc"], "T1")\n'
    ml_repo(tmp_path, 'FEATURES = ["ws", "t"]\n', {"ml/training/train.py": training})

    section = leakcheck.check_features(tmp_path)

    assert section.ok
    assert any("ml/training/train.py:2" in note for note in section.notes)


def test_real_model_features_have_no_scada():
    section = leakcheck.check_features()

    assert section.problems == []
    assert "`ws`" in section.details[0]


# Целиком


def test_main_on_real_cache_exits_zero_and_writes_report(tmp_path, monkeypatch):
    # git_sha и git_dirty паспорта leakcheck все равно обнуляет, а 28 вызовов git на Windows идут десяток секунд.
    monkeypatch.setattr(manifest_module, "_git", lambda *args: None)
    weather_logger = logging.getLogger("src.forecast.weather")
    level = weather_logger.level
    try:
        assert leakcheck.main(["--reports-dir", str(tmp_path)]) == 0
    finally:
        weather_logger.setLevel(level)

    report = (tmp_path / "leakcheck.md").read_text(encoding="utf-8")
    assert "**Итог: утечек не найдено.**" in report
    assert len(list((tmp_path / "manifests").glob("*.json"))) == 28
