"""Паспорт выпуска: сборка, запрет утечки, детерминированность, работа без git."""

import hashlib
import json
import subprocess
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src.forecast.weather import manifest as manifest_module
from src.forecast.weather.manifest import SCADA_FILES, LeakageError, build_manifest, manifest_json, write_manifest

ISSUE = datetime(2026, 2, 1, 2, tzinfo=UTC)


def _nwp() -> pd.DataFrame:
    """48 часов горизонта: первые 24 из свежего GFS, остальные из IFS на цикл раньше."""
    valid = pd.date_range(ISSUE + pd.Timedelta(hours=1), periods=48, freq="h", tz="UTC")
    gfs_init = pd.Timestamp("2026-01-31 18:00", tz="UTC")
    ifs_init = pd.Timestamp("2026-01-31 12:00", tz="UTC")
    rows = []
    for i, ts in enumerate(valid):
        init, delay, source = (gfs_init, pd.Timedelta(hours=7), "gfs") if i < 24 else (ifs_init, pd.Timedelta(hours=7, minutes=30), "ifs")
        rows.append(
            {
                "valid_time_utc": ts,
                "source": source,
                "run_init_utc": init,
                "available_at_utc": init + delay,
                "lead_h": int((ts - init) / pd.Timedelta(hours=1)),
                "ws_hub": 7.5,
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture
def data_dir(tmp_path):
    root = tmp_path / "data"
    for index, name in enumerate(SCADA_FILES.values()):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(f"time,power\n2026-01-01 00:00,0.{index}\n".encode())
    for source in ("gfs", "ifs"):
        cache = root / "nwp" / source
        cache.mkdir(parents=True)
        (cache / "SHA256SUMS").write_bytes(f"{'a' * 64}  {source}_2026013112.json\n{'b' * 64}  {source}_2026013118.json\n".encode())
    return root


def test_manifest_builds_and_serializes(data_dir, tmp_path):
    manifest = build_manifest(ISSUE, _nwp(), data_dir=data_dir, config={"issue_hour_utc": 2})

    assert manifest["issue_time_utc"] == "2026-02-01T02:00:00Z"
    assert manifest["version"] == 1
    assert manifest["max_available_at_utc"] == "2026-02-01T01:00:00Z"
    assert pd.Timestamp(manifest["max_available_at_utc"]) <= pd.Timestamp(manifest["issue_time_utc"])
    assert manifest["decisions"] == []

    assert sorted(manifest["sources"]) == ["gfs", "ifs"]
    gfs, ifs = manifest["sources"]["gfs"], manifest["sources"]["ifs"]
    assert gfs["hours"] == 24 and ifs["hours"] == 24
    assert gfs["lead_h"] == {"min": 1, "max": 24}
    assert ifs["lead_h"] == {"min": 25, "max": 48}
    assert gfs["nwp_lead_h"] == {"min": 9, "max": 32}
    assert ifs["runs"] == [
        {
            "run_init_utc": "2026-01-31T12:00:00Z",
            "available_at_utc": "2026-01-31T19:30:00Z",
            "hours": 24,
            "lead_h": {"min": 25, "max": 48},
            "nwp_lead_h": {"min": 39, "max": 62},
        }
    ]
    sums = (data_dir / "nwp" / "ifs" / "SHA256SUMS").read_bytes()
    assert ifs["cache"] == {"sums_file": "nwp/ifs/SHA256SUMS", "sha256": hashlib.sha256(sums).hexdigest(), "files": 2}

    t1 = (data_dir / SCADA_FILES["T1"]).read_bytes()
    assert manifest["scada"]["T1"] == {"file": SCADA_FILES["T1"], "sha256": hashlib.sha256(t1).hexdigest()}
    assert manifest["scada"]["T2"]["sha256"] is not None
    assert len(manifest["config_sha256"]) == 64

    path = write_manifest(manifest, tmp_path / "out" / "2026-02-01" / "v1" / "manifest.json")
    assert json.loads(path.read_text(encoding="utf-8")) == manifest


def test_row_from_the_future_raises_leakage_error(data_dir):
    nwp = _nwp()
    nwp.loc[5, "available_at_utc"] = pd.Timestamp(ISSUE) + pd.Timedelta(minutes=1)

    with pytest.raises(LeakageError, match="2026-02-01T02:01:00Z"):
        build_manifest(ISSUE, nwp, data_dir=data_dir)


def test_recompute_keeps_issue_time_and_checks_as_of(data_dir):
    as_of = pd.Timestamp(ISSUE) + pd.Timedelta(hours=6)
    nwp = _nwp()
    nwp.loc[:23, "available_at_utc"] = as_of

    manifest = build_manifest(ISSUE, nwp, as_of=as_of, version=2, data_dir=data_dir)

    assert manifest["issue_time_utc"] == "2026-02-01T02:00:00Z"
    assert manifest["as_of_utc"] == "2026-02-01T08:00:00Z"
    assert manifest["max_available_at_utc"] == "2026-02-01T08:00:00Z"
    assert manifest["version"] == 2
    with pytest.raises(LeakageError, match="на момент 2026-02-01T02:00:00Z"):
        build_manifest(ISSUE, nwp, data_dir=data_dir)
    with pytest.raises(LeakageError):
        build_manifest(ISSUE, nwp, as_of=as_of - pd.Timedelta(minutes=1), version=2, data_dir=data_dir)


def test_as_of_defaults_to_issue_time_and_cannot_precede_it(data_dir):
    assert build_manifest(ISSUE, _nwp(), data_dir=data_dir)["as_of_utc"] == "2026-02-01T02:00:00Z"
    with pytest.raises(ValueError, match="раньше момента выпуска"):
        build_manifest(ISSUE, _nwp(), as_of=pd.Timestamp(ISSUE) - pd.Timedelta(hours=1), data_dir=data_dir)


def test_row_without_available_at_raises_leakage_error(data_dir):
    nwp = _nwp()
    nwp["available_at_utc"] = nwp["available_at_utc"].astype("datetime64[ns, UTC]")
    nwp.loc[3, "available_at_utc"] = pd.NaT

    with pytest.raises(LeakageError, match="не задан"):
        build_manifest(ISSUE, nwp, data_dir=data_dir)


def test_available_exactly_at_issue_time_is_allowed(data_dir):
    nwp = _nwp()
    nwp["available_at_utc"] = pd.Timestamp(ISSUE)

    assert build_manifest(ISSUE, nwp, data_dir=data_dir)["max_available_at_utc"] == "2026-02-01T02:00:00Z"


def test_two_calls_give_identical_bytes(data_dir, tmp_path):
    config = {"sources": ["ifs", "gfs"], "issue_time": ISSUE, "delay_h": np.float64(7.5)}
    first = write_manifest(build_manifest(ISSUE, _nwp(), data_dir=data_dir, config=config), tmp_path / "a.json")
    second = write_manifest(build_manifest(ISSUE, _nwp().iloc[::-1], data_dir=data_dir, config=dict(reversed(config.items()))), tmp_path / "b.json")

    assert first.read_bytes() == second.read_bytes()
    assert b"\r\n" not in first.read_bytes()


def test_config_hash_follows_config(data_dir):
    base = build_manifest(ISSUE, _nwp(), data_dir=data_dir, config={"delay_h": 7.5})
    changed = build_manifest(ISSUE, _nwp(), data_dir=data_dir, config={"delay_h": 7.0})
    empty = build_manifest(ISSUE, _nwp(), data_dir=data_dir)

    assert base["config_sha256"] != changed["config_sha256"]
    assert empty["config_sha256"] is None


@pytest.mark.parametrize("error", [FileNotFoundError("git"), subprocess.TimeoutExpired("git", 5), subprocess.CalledProcessError(128, "git")])
def test_manifest_without_git(monkeypatch, data_dir, error):
    def broken_git(*args, **kwargs):
        raise error

    monkeypatch.setattr(manifest_module.subprocess, "run", broken_git)
    manifest = build_manifest(ISSUE, _nwp(), data_dir=data_dir)

    assert manifest["git_sha"] is None
    assert json.loads(manifest_json(manifest))["git_sha"] is None


def test_git_sha_in_repository(data_dir):
    sha = build_manifest(ISSUE, _nwp(), data_dir=data_dir)["git_sha"]

    assert sha is None or len(sha) == 40


def test_missing_cache_and_scada_do_not_fail(tmp_path):
    manifest = build_manifest(ISSUE, _nwp(), data_dir=tmp_path / "empty")

    assert manifest["sources"]["ifs"]["cache"] is None
    assert manifest["scada"]["T1"]["sha256"] is None


def test_no_weather_placeholder_gives_empty_sources(data_dir):
    placeholder = pd.DataFrame(
        {
            "valid_time_utc": pd.date_range(ISSUE + pd.Timedelta(hours=1), periods=48, freq="h", tz="UTC"),
            "source": np.nan,
            "run_init_utc": np.nan,
            "available_at_utc": np.nan,
        }
    )
    manifest = build_manifest(ISSUE, placeholder, data_dir=data_dir)

    assert manifest["sources"] == {}
    assert manifest["max_available_at_utc"] is None


def test_missing_column_is_named(data_dir):
    with pytest.raises(ValueError, match="available_at_utc"):
        build_manifest(ISSUE, _nwp().drop(columns=["available_at_utc"]), data_dir=data_dir)


def test_default_data_dir_hashes_repository_scada(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    manifest = build_manifest(ISSUE, _nwp())

    assert all(len(entry["sha256"]) == 64 for entry in manifest["scada"].values())


@pytest.mark.parametrize("field", ["issue_time", "as_of"])
def test_naive_moment_is_rejected(data_dir, field):
    # 07:00 по Астане без пояса иначе стал бы 07:00 UTC и сдвинул границу утечки на 5 ч.
    naive = datetime(2026, 2, 1, 7, 0)
    kwargs = {"as_of": naive} if field == "as_of" else {}
    issue = naive if field == "issue_time" else ISSUE

    with pytest.raises(ValueError, match="без часового пояса"):
        build_manifest(issue, _nwp(), data_dir=data_dir, **kwargs)


def test_naive_weather_column_is_rejected(data_dir):
    nwp = _nwp()
    nwp["available_at_utc"] = nwp["available_at_utc"].dt.tz_localize(None)

    with pytest.raises(ValueError, match="available_at_utc без часового пояса"):
        build_manifest(ISSUE, nwp, data_dir=data_dir)


@pytest.mark.parametrize(
    ("version", "as_of_shift_h", "message"),
    [
        (1, 30, "версия 1 берет погоду на момент выпуска"),
        (2, 0, "as_of должен быть позже"),
        (0, 0, "нумерация начинается с 1"),
    ],
    ids=["v1-late-as-of", "v2-at-issue-time", "v0"],
)
def test_version_is_bound_to_as_of(data_dir, version, as_of_shift_h, message):
    # Без этой связки версия 1 с as_of = T+30 ч подписывала погоду, вышедшую через 20 ч после выпуска.
    nwp = _nwp()
    nwp["available_at_utc"] = pd.Timestamp(ISSUE) + pd.Timedelta(hours=min(as_of_shift_h, 20))
    as_of = pd.Timestamp(ISSUE) + pd.Timedelta(hours=as_of_shift_h)

    with pytest.raises(ValueError, match=message):
        build_manifest(ISSUE, nwp, as_of=as_of, version=version, data_dir=data_dir)


@pytest.mark.parametrize(
    ("column", "value"),
    [("available_at_utc", pd.Timestamp(ISSUE) + pd.Timedelta(days=3)), ("run_init_utc", pd.Timestamp(ISSUE)), ("ws100", 9.0)],
)
def test_row_without_source_but_with_weather_is_leakage(data_dir, column, value):
    # Раньше такая строка молча выбрасывалась из проверки, а во фрейме для модели оставалась.
    nwp = _nwp().astype({"source": object})
    nwp["ws100"] = 7.5
    nwp.loc[5, ["source", "run_init_utc", "available_at_utc", "ws100"]] = [None, pd.NaT, pd.NaT, np.nan]
    nwp.loc[5, column] = value

    with pytest.raises(LeakageError, match="без source"):
        build_manifest(ISSUE, nwp, data_dir=data_dir)
