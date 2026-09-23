"""Паспорт выпуска прогноза: ``manifest.json``.

Паспорт доказывает, какие прогоны погоды видел выпуск и когда они стали доступны,
и фиксирует всё, от чего зависит результат: кэш погоды, SCADA, код и конфиг.
Собирает его ``build_manifest``, пишет на диск ``write_manifest``. Одинаковый вход
дает побайтно одинаковый файл, поэтому в паспорте нет времени его создания.

Схема (версия ``schema_version`` = 1). Все моменты времени в UTC, строкой
ISO 8601 с ``Z`` на конце: ``"2026-02-01T02:00:00Z"``.

.. code-block:: text

    {
      "schema_version": 1,
      "issue_time_utc": "2026-02-01T02:00:00Z",   # момент выпуска T, одинаковый у всех версий выпуска
      "as_of_utc": "2026-02-01T02:00:00Z",        # момент, на который взята погода: T для версии 1,
                                                  # время выхода нового прогона для пересчетов
      "version": 1,                               # версия выпуска, пересчеты 2, 3, ...
      "max_available_at_utc": "...Z" | null,      # максимум available_at_utc по всем строкам погоды;
                                                  # null, если погоды нет (климатология).
                                                  # Инвариант: max_available_at_utc <= as_of_utc
      "sources": {                                # только источники, реально попавшие в выпуск
        "<source>": {                             # ifs, ifs025, gfs, icon, gem
          "hours": 48,                            # число разных valid_time_utc из этого источника
          "valid_time_utc": {"min": "...Z", "max": "...Z"},
          "lead_h": {"min": 1, "max": 48},        # часы от issue_time_utc до valid_time_utc
          "nwp_lead_h": {"min": 22, "max": 69},   # часы от run_init_utc до valid_time_utc
          "runs": [                               # по возрастанию run_init_utc
            {
              "run_init_utc": "...Z",
              "available_at_utc": "...Z",
              "hours": 48,
              "lead_h": {"min": 1, "max": 48},
              "nwp_lead_h": {"min": 22, "max": 69}
            }
          ],
          "cache": {                              # null, если SHA256SUMS источника не найден
            "sums_file": "nwp/<source>/SHA256SUMS",   # путь относительно DATA_DIR
            "sha256": "<hex>",                    # sha256 самого SHA256SUMS: закрепляет все файлы кэша
            "files": 120                          # сколько файлов перечислено в SHA256SUMS
          }
        }
      },
      "scada": {
        "T1": {"file": "Dataset HackAlemAI turbine 1.csv", "sha256": "<hex>" | null},
        "T2": {"file": "Dataset HackAlemAI turbine 2.csv", "sha256": "<hex>" | null}
      },
      "git_sha": "<40 hex>" | null,               # null, если git недоступен (например, в контейнере)
      "config_sha256": "<hex>" | null,            # sha256 канонического JSON конфига, null без конфига
      "decisions": []                             # журнал решений агента, заполняет dev1
    }

Хэши файлов кэша берутся из ``SHA256SUMS``, а не пересчитываются: в паспорт
идет sha256 самого файла сумм, и по нему однозначно проверяется весь кэш источника.

Строка погоды с ``available_at_utc`` позже ``as_of_utc`` означает утечку
будущего, и паспорт такой выпуск не подписывает: ``LeakageError``. Проверка
утечек по готовым паспортам (#20) сравнивает с тем же ``as_of_utc``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.forecast.weather.asof import LeakageError, to_utc

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

REQUIRED_COLUMNS: tuple[str, ...] = ("valid_time_utc", "source", "run_init_utc", "available_at_utc")

SCADA_FILES: dict[str, str] = {
    "T1": "Dataset HackAlemAI turbine 1.csv",
    "T2": "Dataset HackAlemAI turbine 2.csv",
}

NWP_CACHE_DIR = "nwp"
SUMS_FILE = "SHA256SUMS"

GIT_TIMEOUT_S = 5

_HASH_CHUNK = 1 << 20
_REPO_ROOT = Path(__file__).resolve().parents[4]


def build_manifest(
    issue_time: datetime | pd.Timestamp | str,
    nwp: pd.DataFrame,
    *,
    as_of: datetime | pd.Timestamp | str | None = None,
    version: int = 1,
    config: Any = None,
    data_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Паспорт выпуска ``issue_time`` по таблице погоды ``nwp`` из ``get_nwp``.

    ``as_of`` задается при пересчете: момент, на который взята погода. По умолчанию
    совпадает с ``issue_time`` и не может быть раньше него. Время без часового пояса
    отклоняется, как и в ``AsOfStore``: иначе местное время молча станет UTC.
    Строки ``nwp`` без ``source`` считаются заглушкой «погоды нет» и в паспорт
    не попадают. ``data_dir`` по умолчанию берется из ``DATA_DIR``, иначе
    ``data/`` в корне репозитория.
    """
    issue = to_utc(issue_time)
    moment = to_utc(as_of) if as_of is not None else issue
    if moment < issue:
        raise ValueError(f"as_of {_iso(moment)} раньше момента выпуска {_iso(issue)}")
    root = _data_dir(data_dir)
    frame = _prepare(nwp)

    unknown = frame["available_at_utc"].isna()
    if unknown.any():
        sources = sorted(frame.loc[unknown, "source"].astype(str).unique())
        raise LeakageError(f"available_at_utc не задан у {int(unknown.sum())} строк погоды, источники {sources}")

    max_available = frame["available_at_utc"].max() if not frame.empty else None
    if max_available is not None and max_available > moment:
        late = frame.loc[frame["available_at_utc"] > moment]
        runs = sorted({_iso(ts) for ts in late["run_init_utc"]})
        raise LeakageError(
            f"выпуск {_iso(issue)} на момент {_iso(moment)} видит {len(late)} строк погоды, опубликованных позже: "
            f"max available_at_utc {_iso(max_available)}, прогоны {runs}"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "issue_time_utc": _iso(issue),
        "as_of_utc": _iso(moment),
        "version": int(version),
        "max_available_at_utc": _iso(max_available) if max_available is not None else None,
        "sources": {source: _describe_source(rows, issue, root, source) for source, rows in frame.groupby("source", sort=True)},
        "scada": {turbine: {"file": name, "sha256": _file_sha256(root / name)} for turbine, name in SCADA_FILES.items()},
        "git_sha": _git_sha(),
        "config_sha256": _sha256_bytes(_canonical_json(config).encode("utf-8")) if config is not None else None,
        "decisions": [],
    }


def manifest_json(manifest: dict[str, Any]) -> str:
    """Каноническая сериализация паспорта: ключи отсортированы, время в ISO 8601 с ``Z``."""
    return json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False, default=_json_default) + "\n"


def write_manifest(manifest: dict[str, Any], path: str | Path) -> Path:
    """Пишет паспорт в ``path``. Байты не зависят от ОС: UTF-8 и переводы строк ``\\n``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(manifest_json(manifest).encode("utf-8"))
    return target


def _prepare(nwp: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in nwp.columns]
    if missing:
        raise ValueError(f"в таблице погоды нет колонок {missing}")

    frame = nwp.loc[nwp["source"].notna(), list(REQUIRED_COLUMNS)].copy()
    for column in ("valid_time_utc", "run_init_utc", "available_at_utc"):
        if pd.api.types.is_datetime64_dtype(frame[column]) and not isinstance(frame[column].dtype, pd.DatetimeTZDtype):
            raise ValueError(f"колонка {column} без часового пояса, нужен UTC")
        frame[column] = pd.to_datetime(frame[column], utc=True)

    for column in ("valid_time_utc", "run_init_utc"):
        if frame[column].isna().any():
            raise ValueError(f"в таблице погоды пустые значения в колонке {column}")
    frame["source"] = frame["source"].astype(str)
    return frame


def _describe_source(rows: pd.DataFrame, issue: pd.Timestamp, root: Path, source: str) -> dict[str, Any]:
    runs = [
        {
            "run_init_utc": _iso(run_init),
            "available_at_utc": _iso(available_at),
            "hours": int(run_rows["valid_time_utc"].nunique()),
            "lead_h": _hours_range(run_rows["valid_time_utc"], issue),
            "nwp_lead_h": _hours_range(run_rows["valid_time_utc"], run_rows["run_init_utc"]),
        }
        for (run_init, available_at), run_rows in rows.groupby(["run_init_utc", "available_at_utc"], sort=True)
    ]
    return {
        "hours": int(rows["valid_time_utc"].nunique()),
        "valid_time_utc": {"min": _iso(rows["valid_time_utc"].min()), "max": _iso(rows["valid_time_utc"].max())},
        "lead_h": _hours_range(rows["valid_time_utc"], issue),
        "nwp_lead_h": _hours_range(rows["valid_time_utc"], rows["run_init_utc"]),
        "runs": runs,
        "cache": _cache_digest(root, source),
    }


def _hours_range(valid_times: pd.Series, start: pd.Series | pd.Timestamp) -> dict[str, int]:
    hours = (valid_times - start) / pd.Timedelta(hours=1)
    return {"min": int(np.floor(hours.min())), "max": int(np.floor(hours.max()))}


def _cache_digest(root: Path, source: str) -> dict[str, Any] | None:
    sums = root / NWP_CACHE_DIR / source / SUMS_FILE
    if not sums.is_file():
        logger.warning("manifest: нет %s для источника %s, хэш кэша не записан", sums, source)
        return None
    content = sums.read_bytes()
    files = sum(1 for line in content.decode("utf-8").splitlines() if line.strip())
    return {"sums_file": f"{NWP_CACHE_DIR}/{source}/{SUMS_FILE}", "sha256": _sha256_bytes(content), "files": files}


def _file_sha256(path: Path) -> str | None:
    if not path.is_file():
        logger.warning("manifest: файл %s не найден, sha256 не записан", path)
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _git_sha() -> str | None:
    """``git rev-parse HEAD`` или ``None``, если git или репозитория нет."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.info("manifest: git sha недоступен, в паспорт пишется null")
        return None
    return result.stdout.strip() or None


def _data_dir(data_dir: str | Path | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    return Path(os.environ.get("DATA_DIR") or _REPO_ROOT / "data")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp | datetime):
        return _iso(_utc(value))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, set | frozenset):
        return sorted(value)
    raise TypeError(f"значение типа {type(value).__name__} не сериализуется в паспорт")


def _utc(value: datetime | pd.Timestamp | str) -> pd.Timestamp:
    """Момент в UTC для сериализации; время без пояса считается UTC."""
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(value: pd.Timestamp) -> str:
    return _utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")
