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
      "missing_sources": ["ifs"] | null,          # запрошенные источники, которых нет в выпуске
                                                  # (нет прогона на as_of); null, если запрос не передан
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
            "files": 25                           # сколько файлов перечислено в SHA256SUMS
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

В паспорт идет sha256 самого ``SHA256SUMS``, а перед этим каждый перечисленный
в нем файл сверяется со своей суммой. Кэш, который разошелся с ``SHA256SUMS``,
и ``*.csv.gz``, которого в нем нет (``AsOfStore`` прочитал бы его), дают
``ValueError``: такой паспорт закрепил бы не те данные, на которых считался выпуск.

Строка погоды с ``available_at_utc`` позже ``as_of_utc`` означает утечку
будущего, и паспорт такой выпуск не подписывает: ``LeakageError``. Проверка
утечек по готовым паспортам (#20) сравнивает с тем же ``as_of_utc``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import subprocess
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.forecast.dataset import config as dataset_config
from src.forecast.dataset.config import SCADA_FILES
from src.forecast.weather.asof import VALUE_COLUMNS, LeakageError, to_utc
from src.forecast.weather.sources import SOURCES, Source

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

REQUIRED_COLUMNS: tuple[str, ...] = ("valid_time_utc", "source", "run_init_utc", "available_at_utc")


NWP_CACHE_DIR = "nwp"
SUMS_FILE = "SHA256SUMS"

GIT_TIMEOUT_S = 5

_HASH_CHUNK = 1 << 20


def build_manifest(
    issue_time: datetime | pd.Timestamp | str,
    nwp: pd.DataFrame,
    *,
    as_of: datetime | pd.Timestamp | str | None = None,
    version: int = 1,
    requested_sources: Sequence[str] | None = None,
    config: Any = None,
    data_dir: str | Path | None = None,
    sources: Mapping[str, Source] | None = None,
) -> dict[str, Any]:
    """Паспорт выпуска ``issue_time`` по таблице погоды ``nwp`` из ``get_nwp``.

    ``as_of`` задается при пересчете: момент, на который взята погода. Версия 1
    всегда берет погоду ровно на момент выпуска, поэтому ``as_of`` у нее равен
    ``issue_time``; пересчет (версия 2 и выше) идет строго позже выпуска. Время без
    часового пояса отклоняется, как и в ``AsOfStore``: иначе местное время молча
    станет UTC. Строки ``nwp`` без ``source`` считаются заглушкой «погоды нет» и в
    паспорт не попадают, если в них нет ни прогона, ни значений погоды; иначе
    их время публикации не проверить, и это ``LeakageError``.

    ``data_dir`` по умолчанию тот же, что у загрузки SCADA: ``DATA_DIR``
    из ``dataset/config.py``.

    ``requested_sources`` — источники, которые выпуск запрашивал у ``get_nwp_multi``.
    Те из них, что пропущены без прогона, попадают в ``missing_sources``.

    ``available_at_utc`` не берется на веру: у источника из реестра ``sources``
    (по умолчанию ``SOURCES``, как у ``AsOfStore``) он не раньше ``run_init_utc``
    плюс задержка публикации, у неизвестного источника — не раньше ``run_init_utc``.
    """
    issue = to_utc(issue_time)
    moment = to_utc(as_of) if as_of is not None else issue
    _check_version(int(version), issue, moment)
    root = _data_dir(data_dir)
    frame = _prepare(nwp)

    unknown = frame["available_at_utc"].isna()
    if unknown.any():
        names = sorted(frame.loc[unknown, "source"].astype(str).unique())
        raise LeakageError(f"available_at_utc не задан у {int(unknown.sum())} строк погоды, источники {names}")
    _check_publication_delay(frame, SOURCES if sources is None else sources)

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
        "missing_sources": sorted(set(requested_sources) - set(frame["source"])) if requested_sources is not None else None,
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


def _check_version(version: int, issue: pd.Timestamp, moment: pd.Timestamp) -> None:
    """Версия 1 видит погоду ровно на момент выпуска, пересчет — строго позже него."""
    if version < 1:
        raise ValueError(f"версия выпуска {version}, нумерация начинается с 1")
    if moment < issue:
        raise ValueError(f"as_of {_iso(moment)} раньше момента выпуска {_iso(issue)}")
    if version == 1 and moment != issue:
        raise ValueError(f"версия 1 берет погоду на момент выпуска {_iso(issue)}, а не на {_iso(moment)}")
    if version > 1 and moment == issue:
        raise ValueError(f"пересчет (версия {version}) идет по прогону, вышедшему после выпуска {_iso(issue)}, as_of должен быть позже")


def _check_publication_delay(frame: pd.DataFrame, sources: Mapping[str, Source]) -> None:
    """``available_at_utc`` не раньше, чем прогон мог выйти по реестру источников."""
    delay = frame["source"].map(lambda name: sources[name].delay if name in sources else pd.Timedelta(0))
    early = frame["available_at_utc"] < frame["run_init_utc"] + pd.to_timedelta(delay)
    if early.any():
        first = frame.loc[early].iloc[0]
        raise LeakageError(
            f"{int(early.sum())} строк погоды доступны раньше, чем прогон мог выйти: например {first['source']} "
            f"прогон {_iso(first['run_init_utc'])} с available_at_utc {_iso(first['available_at_utc'])}"
        )


def _prepare(nwp: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in nwp.columns]
    if missing:
        raise ValueError(f"в таблице погоды нет колонок {missing}")

    no_source = nwp["source"].isna()
    provenance = ["run_init_utc", "available_at_utc", *(column for column in VALUE_COLUMNS if column in nwp.columns)]
    orphan = no_source & nwp[provenance].notna().any(axis=1)
    if orphan.any():
        raise LeakageError(f"{int(orphan.sum())} строк погоды без source, но с прогоном или значениями: время их публикации не проверить")

    frame = nwp.loc[~no_source, list(REQUIRED_COLUMNS)].copy()
    for column in ("valid_time_utc", "run_init_utc", "available_at_utc"):
        if pd.api.types.is_datetime64_dtype(frame[column]) and not isinstance(frame[column].dtype, pd.DatetimeTZDtype):
            raise ValueError(f"колонка {column} без часового пояса, нужен UTC")
        frame[column] = pd.to_datetime(frame[column], utc=True)
        # В паспорте время с точностью до секунды: дробь дала бы разные прогоны с одинаковой записью.
        if (frame[column].notna() & (frame[column] != frame[column].dt.floor("s"))).any():
            raise ValueError(f"в колонке {column} время с долями секунды, нужна точность до секунды")

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
    listed = _parse_sums(content.decode("utf-8"))
    folder = sums.parent
    unlisted = sorted(path.name for path in folder.glob("*.csv.gz") if path.name not in listed)
    if unlisted:
        raise ValueError(f"в кэше {source} файлы вне {SUMS_FILE}: {unlisted}")
    broken = sorted(name for name, digest in listed.items() if _file_sha256(folder / name) != digest)
    if broken:
        raise ValueError(f"кэш {source} не совпадает с {SUMS_FILE} или файлов нет: {broken}")
    return {"sums_file": f"{NWP_CACHE_DIR}/{source}/{SUMS_FILE}", "sha256": _sha256_bytes(content), "files": len(listed)}


def _parse_sums(text: str) -> dict[str, str]:
    """Строки ``sha256sum``: ``<hex>  <имя>`` или ``<hex> *<имя>`` в двоичном режиме."""
    listed = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        listed[name.strip().removeprefix("*")] = digest.lower()
    return listed


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
    return dataset_config.DATA_DIR


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp | datetime):
        return _iso(_utc(value))
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, timedelta):
        return pd.Timedelta(value).isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
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
