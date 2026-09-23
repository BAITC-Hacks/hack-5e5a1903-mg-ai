"""Проверка утечек будущего: ``make leakcheck``.

Запуск из папки backend::

    uv run python -m src.forecast.leakcheck

Код выхода 0 — утечек нет, 1 — найдены. Готовых выпусков в ``outputs/`` больше нет
(План 2, #37), поэтому проверка ничего не берет из файлов на веру: погоду каждого
выпуска она сама получает через ``AsOfStore`` и сверяет ее независимо от него.

1. Запрещенные API. Код ``backend/src``, ``ml/src``, ``ml/training``, ``ml/scripts``
   и конфиги compose не обращаются к ERA5, Historical Weather API (``archive-api``)
   и Historical Forecast API. Смотрятся строки и имена в коде, но не докстринги
   и комментарии: объяснять, почему ERA5 запрещен, можно. Диагностика
   ``backend/src/analysis`` — разрешенное исключение, но ее не импортирует
   ни один проверяемый модуль.
2. Выпуски февраля 2026: 28 выпусков 31.01–27.02 в 02:00 UTC, все источники из
   ``sources.py``, часы T+1…T+48. В каждой строке погоды ``available_at_utc <= T``,
   ``run_init_utc <= available_at_utc``, а время доступности пересчитывается
   из ``run_init_utc`` и задержки источника, а не берется из колонки. Паспорт
   каждого выпуска (``build_manifest``) пишется в ``reports/manifests/<дата>.json``.
3. Обучение: те же проверки для выпуска 02:00 UTC каждого дня периода SCADA.
   ``ml/training/train.py`` строит по ним проверку Б и калибровку P10/P90.
   Признаки модели на инференсе не содержат значений SCADA и лагов.
4. Previous Runs: метка прогона в кэше совпадает с ``prev_runs_rule.run_init_for``,
   а ``choose_n`` с задержкой из ``sources.py`` не выбирает прогон новее выпуска,
   в том числе на граничных часах +24, +25, +48.

Отчет ``reports/leakcheck.md`` пишет этот модуль. Отчет и паспорта детерминированы:
в них нет времени запуска, а ``git_sha`` и ``git_dirty`` паспорта здесь пустые.
Файл коммитится вместе с кодом, и свой собственный коммит в нем не записать,
иначе каждый коммит менял бы все 28 паспортов.
"""

import argparse
import ast
import logging
import re
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.forecast.dataset import config as dataset_config
from src.forecast.dataset.config import SCADA_FILES
from src.forecast.dataset.scada import CSV_HEADERS, MEASURES
from src.forecast.weather.asof import AsOfStore, LeakageError, NoRunAvailable
from src.forecast.weather.fetch_prev_runs import MODELS, PrevRunsModel
from src.forecast.weather.manifest import NWP_CACHE_DIR, build_manifest, write_manifest
from src.forecast.weather.prev_runs_rule import PREV_DAYS, choose_n, run_init_for
from src.forecast.weather.sources import SOURCES, Source

logger = logging.getLogger(__name__)

REPO = dataset_config.REPO

ISSUE_HOUR_UTC = 2
HORIZON_H = 48
FEB_ISSUES = ("2026-01-31", "2026-02-27")
# Период SCADA из GOAL.md. train.py выпускает прогноз на 02:00 UTC каждого дня с данными
# SCADA, поэтому все календарные дни периода покрывают его выборку с запасом.
TRAIN_ISSUES = ("2023-03-11", "2026-01-31")
BOUNDARY_LEADS = (24, 25, 48)
MAX_EXAMPLES = 5

SCAN_ROOTS = ("backend/src", "ml/src", "ml/training", "ml/scripts")
SCAN_FILES = (".env.example", "docker-compose.yml", "docker-compose.dev.yml")
TEXT_SUFFIXES = {".json", ".toml", ".yaml", ".yml", ".cfg", ".ini", ".txt", ".sql", ".env"}
FORBIDDEN = {
    "Historical Weather API": re.compile(r"archive-api|/v1/archive\b", re.I),
    "Historical Forecast API": re.compile(r"historical[-_]forecast", re.I),
    "реанализ ERA5": re.compile(r"(?<![a-z0-9])era5", re.I),
    "реанализ CERRA": re.compile(r"(?<![a-z0-9])cerra(?![a-z0-9])", re.I),
}
# Разрешенные исключения: путь от корня репозитория -> причина.
DIAGNOSTICS = {
    "backend/src/analysis": (
        "диагностика часового пояса и высоты ветра по ERA5 (reports/tz_check.md, reports/weather_vs_scada.md); "
        "в прогноз не попадает, ни один проверяемый модуль ее не импортирует"
    ),
}
SELF = Path(__file__).resolve()
SELF_REASON = "здесь сам список запрещенных шаблонов"

INFERENCE_PACKAGE = "ml/src/ml_service"
FEATURE_MODULES = ("ml/src/ml_service/features.py", "ml/src/ml_service/frame.py")
TRAINING_DIR = "ml/training"
SCADA_MODULE = "src.forecast.dataset"
SCADA_LOADERS = {"load_scada", "read_turbine_csv", "aggregate_hourly"}
SCADA_MARKERS = (*SCADA_FILES.values(), *CSV_HEADERS)
SUSPICIOUS_FEATURE = re.compile(r"power|lag|actual|fact|scada|persist|shift", re.I)
LAG_CALLS = {"shift", "diff", "rolling", "ewm", "expanding"}


@dataclass
class Section:
    """Одна проверка отчета: что проверено, утечки (код выхода 1) и замечания (не влияют на код выхода)."""

    title: str
    checked: str = ""
    details: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass
class IssueCheck:
    """Итог проверки строк погоды по набору выпусков."""

    issues: int = 0
    with_weather: int = 0
    rows: int = 0
    rows_by_source: Counter = field(default_factory=Counter)
    missing_sources: Counter = field(default_factory=Counter)
    no_weather: list[pd.Timestamp] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    frames: dict[pd.Timestamp, pd.DataFrame] = field(default_factory=dict)


def _fmt(ts: pd.Timestamp) -> str:
    return f"{pd.Timestamp(ts).tz_convert('UTC'):%Y-%m-%d %H:%M}"


def _rel(path: Path, repo: Path) -> str:
    return path.resolve().relative_to(repo.resolve()).as_posix()


def issue_times(first: str, last: str) -> pd.DatetimeIndex:
    """Выпуски в ``ISSUE_HOUR_UTC`` каждого дня от ``first`` до ``last`` включительно."""
    return pd.date_range(first, last, freq="D", tz="UTC") + pd.Timedelta(hours=ISSUE_HOUR_UTC)


def horizon(issue: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(issue + pd.Timedelta(hours=1), periods=HORIZON_H, freq="h")


# 1. Запрещенные API


def _docstring_ids(tree: ast.AST) -> set[int]:
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _code_tokens(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Строки и имена из кода без докстрингов: (номер строки, текст). Комментариев в AST нет."""
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            yield node.lineno, node.value
        elif isinstance(node, ast.Name):
            yield node.lineno, node.id
        elif isinstance(node, ast.Attribute):
            yield node.lineno, node.attr
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node.lineno, node.name
        elif isinstance(node, ast.arg):
            yield node.lineno, node.arg
        elif isinstance(node, ast.keyword) and node.arg:
            yield node.lineno, node.arg
    for lineno, module in _imports(tree):
        yield lineno, module


def _imports(tree: ast.AST) -> Iterator[tuple[int, str]]:
    """Импортируемые имена: ``import a.b`` дает ``a.b``, ``from a import b`` дает ``a.b``."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                yield node.lineno, f"{node.module}.{alias.name}"


def _allowed(rel: str, allowed: Mapping[str, str]) -> str | None:
    for prefix, reason in allowed.items():
        if rel == prefix or rel.startswith(f"{prefix}/"):
            return reason
    return None


def _module_name(prefix: str) -> str:
    """``backend/src/analysis`` -> ``src.analysis``: так его импортирует код backend и ml."""
    return ".".join(Path(prefix).with_suffix("").parts[1:])


def _scan_files(repo: Path, roots: Sequence[str], files: Sequence[str]) -> list[Path]:
    found = [repo / name for name in files if (repo / name).is_file()]
    for root in roots:
        folder = repo / root
        if folder.is_dir():
            found.extend(
                path
                for path in folder.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts and (path.suffix == ".py" or path.suffix in TEXT_SUFFIXES)
            )
    return sorted(set(found))


def scan_forbidden_apis(
    repo: Path = REPO,
    roots: Sequence[str] = SCAN_ROOTS,
    files: Sequence[str] = SCAN_FILES,
    allowed: Mapping[str, str] = DIAGNOSTICS,
) -> Section:
    """Ищет обращения к реанализу и архиву фактической погоды в коде и конфигах."""
    section = Section("1. Запрещенные API")
    skipped: dict[str, list[str]] = {}
    python, text = 0, 0
    banned_modules = {prefix: _module_name(prefix) for prefix in allowed}
    for path in _scan_files(repo, roots, files):
        rel = _rel(path, repo)
        if path.resolve() == SELF:
            skipped.setdefault(SELF_REASON, []).append(rel)
            continue
        reason = _allowed(rel, allowed)
        if reason is not None:
            skipped.setdefault(reason, []).append(rel)
            continue
        if path.suffix == ".py":
            python += 1
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            tokens = list(_code_tokens(tree))
            for lineno, module in _imports(tree):
                for prefix, name in banned_modules.items():
                    if module == name or module.startswith(f"{name}."):
                        section.problems.append(f"`{rel}:{lineno}` импортирует `{module}` из разрешенного исключения `{prefix}`")
        else:
            text += 1
            tokens = list(enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1))
        for lineno, value in tokens:
            for label, pattern in FORBIDDEN.items():
                if pattern.search(value):
                    section.problems.append(f"`{rel}:{lineno}`: {label}, `{value.strip()[:120]}`")

    section.checked = f"файлов Python: {python}, конфигов: {text}; места: {', '.join(f'`{r}`' for r in (*roots, *files))}"
    section.details.append("Шаблоны: " + "; ".join(f"{label} `{pattern.pattern}`" for label, pattern in FORBIDDEN.items()) + ".")
    for reason, paths in skipped.items():
        section.details.append(f"Исключение ({', '.join(f'`{path}`' for path in paths)}): {reason}.")
    section.problems = sorted(set(section.problems))
    return section


# 2 и 3. Строки погоды выпусков


def check_rows(nwp: pd.DataFrame, issue: pd.Timestamp, sources: Mapping[str, Source] = SOURCES) -> list[str]:
    """Независимая проверка строк погоды одного выпуска.

    Не доверяет ни ``AsOfStore``, ни колонке ``available_at_utc``: время доступности
    пересчитывается из ``run_init_utc`` и задержки источника.
    """
    problems = []
    known = nwp["source"].isin(list(sources))
    delay = pd.to_timedelta(nwp["source"].map({name: source.delay for name, source in sources.items()}))
    earliest = nwp["run_init_utc"] + delay

    def flag(mask: pd.Series, what: str) -> None:
        if mask.any():
            first = nwp.loc[mask].iloc[0]
            problems.append(
                f"выпуск {_fmt(issue)}: {int(mask.sum())} строк {what}, например {first['source']} "
                f"прогон {_fmt(first['run_init_utc'])} на {_fmt(first['valid_time_utc'])}, доступен {_fmt(first['available_at_utc'])}"
            )

    flag(~known, "неизвестного источника, задержку публикации не проверить")
    flag(nwp["available_at_utc"] > issue, "опубликованы позже выпуска")
    flag(nwp["run_init_utc"] > nwp["available_at_utc"], "с run_init_utc позже available_at_utc")
    flag(known & (nwp["available_at_utc"] < earliest), "доступны раньше, чем прогон мог выйти по задержке источника")
    flag(known & (earliest > issue), "из прогона, который по задержке источника выходит позже выпуска")
    flag(~nwp["valid_time_utc"].isin(horizon(issue)), f"вне горизонта T+1…T+{HORIZON_H}")
    flag(nwp.duplicated(["source", "valid_time_utc"], keep=False), "повторяются на один час и источник")
    return problems


def check_issues(store: AsOfStore, issues: Sequence[pd.Timestamp], sources: Mapping[str, Source] = SOURCES, keep_frames: bool = False) -> IssueCheck:
    """Погода каждого выпуска через ``get_nwp_multi`` и ``check_rows`` по ней."""
    result = IssueCheck()
    names = list(sources)
    for issue in issues:
        result.issues += 1
        try:
            nwp = store.get_nwp_multi(names, issue, horizon(issue))
        except NoRunAvailable:
            result.no_weather.append(issue)
            continue
        except LeakageError as error:
            result.problems.append(f"выпуск {_fmt(issue)}: AsOfStore остановил утечку: {error}")
            continue
        result.with_weather += 1
        result.rows += len(nwp)
        result.rows_by_source.update(nwp["source"].value_counts().to_dict())
        result.missing_sources.update(set(names) - set(nwp["source"]))
        result.problems.extend(check_rows(nwp, issue, sources))
        if keep_frames:
            result.frames[issue] = nwp
    return result


def _rows_line(check: IssueCheck) -> str:
    by_source = ", ".join(f"{name} {count}" for name, count in sorted(check.rows_by_source.items()))
    return f"Строк погоды: {check.rows}; {by_source}." if check.rows else "Строк погоды нет."


def _manifest_config(sources: Mapping[str, Source]) -> dict[str, Any]:
    return {"issue_hour_utc": ISSUE_HOUR_UTC, "horizon_h": HORIZON_H, "sources": [sources[name] for name in sorted(sources)]}


def write_manifests(
    frames: Mapping[pd.Timestamp, pd.DataFrame], out_dir: Path, data_dir: Path, sources: Mapping[str, Source] = SOURCES
) -> tuple[list[dict[str, Any]], list[str]]:
    """Паспорт каждого выпуска в ``out_dir/<дата>.json``. Возвращает строки таблицы отчета и утечки."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.json"):
        old.unlink()
    rows, problems = [], []
    for issue, nwp in sorted(frames.items()):
        try:
            manifest = build_manifest(
                issue, nwp, requested_sources=list(sources), config=_manifest_config(sources), data_dir=data_dir, sources=sources
            )
        except (LeakageError, ValueError) as error:
            problems.append(f"выпуск {_fmt(issue)}: паспорт не собран: {error}")
            continue
        latest = manifest["max_available_at_utc"]
        if latest is not None and pd.Timestamp(latest) > issue:
            problems.append(f"выпуск {_fmt(issue)}: max_available_at_utc {latest} позже выпуска")
        manifest["git_sha"] = None
        manifest["git_dirty"] = None
        path = write_manifest(manifest, out_dir / f"{issue:%Y-%m-%d}.json")
        slack = (issue - pd.Timestamp(latest)) / pd.Timedelta(hours=1) if latest is not None else None
        last = nwp.loc[nwp["available_at_utc"].idxmax()] if len(nwp) else None
        rows.append(
            {
                "issue": _fmt(issue),
                "rows": len(nwp),
                "sources": ", ".join(manifest["sources"]),
                "missing": ", ".join(manifest["missing_sources"] or []) or "—",
                "latest": _fmt(pd.Timestamp(latest)) if latest is not None else "—",
                "last_run": f"{last['source']} {last['run_init_utc']:%d.%m %H}z" if last is not None else "—",
                "slack": f"{slack:.1f}" if slack is not None else "—",
                "file": path.name,
            }
        )
    return rows, problems


# 4. Previous Runs


def _utc_index(values: pd.Series) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True, format="ISO8601"))


def _ns(index: pd.DatetimeIndex) -> np.ndarray:
    return index.tz_convert("UTC").tz_localize(None).as_unit("ns").to_numpy().view("i8")


def check_prev_runs_labels(
    cache_root: Path, models: Mapping[str, PrevRunsModel] = MODELS, sources: Mapping[str, Source] = SOURCES
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Метка ``run_init_utc`` каждой строки кэша против ``run_init_for(valid, prev_day)``.

    Метка старше правила — утечка: ``AsOfStore`` посчитает прогон вышедшим раньше,
    чем вышел самый новый прогон, чьи значения попали в час. Метка новее правила
    только осторожнее, это замечание.
    """
    rows, problems, notes = [], [], []
    for name, model in models.items():
        source = sources.get(name)
        if source is None:
            problems.append(f"{name} грузится из Previous Runs, но его нет в sources.py: задержка публикации не задана")
            continue
        if model.cycle_h != source.run_step_h:
            problems.append(f"{name}: шаг прогонов {model.cycle_h} ч в загрузчике и {source.run_step_h} ч в sources.py")
        files = sorted((cache_root / source.cache_dir).glob("*.csv.gz"))
        if not files:
            notes.append(f"{name}: кэша нет, метки не проверены")
            continue
        raw = pd.concat([pd.read_csv(path, usecols=lambda column: column in {"run_init_utc", "valid_time_utc", "prev_day"}) for path in files])
        if "prev_day" not in raw.columns:
            problems.append(f"{name}: в кэше нет prev_day, метку прогона не проверить")
            continue
        valid, labelled = _utc_index(raw["valid_time_utc"]), _utc_index(raw["run_init_utc"])
        prev = raw["prev_day"].to_numpy()
        expected = np.full(len(raw), np.iinfo(np.int64).min)
        for n in PREV_DAYS:
            mask = prev == n
            expected[mask] = _ns(run_init_for(valid[mask], n, model.cycle_h, model.data_step_h))
        known = np.isin(prev, PREV_DAYS)
        older = known & (_ns(labelled) < expected)
        newer = known & (_ns(labelled) > expected)
        if (~known).any():
            problems.append(f"{name}: {int((~known).sum())} строк с prev_day вне {PREV_DAYS}, прогон не определить")
        if older.any():
            i = int(np.flatnonzero(older)[0])
            problems.append(
                f"{name}: {int(older.sum())} строк помечены прогоном старше правила, например час {_fmt(valid[i])} "
                f"previous_day{prev[i]}: в кэше {_fmt(labelled[i])}, по правилу {_fmt(pd.Timestamp(expected[i], tz='UTC'))}"
            )
        if newer.any():
            notes.append(f"{name}: {int(newer.sum())} строк помечены прогоном новее правила, это только осторожнее")
        rows.append(
            {
                "source": name,
                "rows": len(raw),
                "cycle": model.cycle_h,
                "step": model.data_step_h,
                "delay": _hours(source.delay),
                "older": int(older.sum()),
                "newer": int(newer.sum()),
            }
        )
    return rows, problems, notes


def _hours(delay) -> str:
    hours = pd.Timedelta(delay) / pd.Timedelta(hours=1)
    return f"{hours:g} ч"


def check_prev_runs_boundaries(
    feb_issues: Sequence[pd.Timestamp],
    train_issues: Sequence[pd.Timestamp],
    feb_frames: Mapping[pd.Timestamp, pd.DataFrame],
    models: Mapping[str, PrevRunsModel] = MODELS,
    sources: Mapping[str, Source] = SOURCES,
) -> tuple[list[dict[str, Any]], list[str], int]:
    """``choose_n`` + задержка не дают прогон новее выпуска.

    Февраль — все часы +1…+48, обучение — граничные часы ``BOUNDARY_LEADS``.
    Для февраля еще сверка с тем, что реально выбрал ``AsOfStore``: он не берет
    прогон новее, чем разрешает правило.
    """
    problems: list[str] = []
    checked = 0
    stats: dict[tuple[str, int], dict[str, Any]] = {}
    chosen = {
        (issue, row.source, row.valid_time_utc): row.run_init_utc
        for issue, frame in feb_frames.items()
        for row in frame[["source", "valid_time_utc", "run_init_utc"]].itertuples(index=False)
    }
    plan = [(issue, lead, True) for issue in feb_issues for lead in range(1, HORIZON_H + 1)]
    plan += [(issue, lead, False) for issue in train_issues for lead in BOUNDARY_LEADS]
    for name, model in models.items():
        if name not in sources:
            continue
        delay = pd.Timedelta(sources[name].delay)
        for issue, lead, is_feb in plan:
            t = issue + pd.Timedelta(hours=lead)
            checked += 1
            n = choose_n(t, issue, model.cycle_h, delay, model.data_step_h)
            run = run_init_for(t, n, model.cycle_h, model.data_step_h) if n is not None else None
            if run is not None and run + delay > issue:
                problems.append(f"{name}, выпуск {_fmt(issue)}, +{lead} ч: previous_day{n} это прогон {_fmt(run)}, выходит {_fmt(run + delay)}")
            store_run = chosen.get((issue, name, t)) if is_feb else None
            if store_run is not None and (run is None or store_run > run):
                allowed = _fmt(run) if run is not None else "никакой"
                problems.append(
                    f"{name}, выпуск {_fmt(issue)}, +{lead} ч: AsOfStore взял прогон {_fmt(store_run)}, правило разрешает не новее {allowed}"
                )
            if lead not in BOUNDARY_LEADS:
                continue
            item = stats.setdefault((name, lead), {"n": Counter(), "slack": None, "naive": 0, "issues": 0, "none": 0})
            item["issues"] += 1
            item["naive"] += int(run_init_for(t, 1, model.cycle_h, model.data_step_h) + delay > issue)
            if run is None:
                item["none"] += 1
                continue
            item["n"][n] += 1
            slack = (issue - run - delay) / pd.Timedelta(hours=1)
            item["slack"] = slack if item["slack"] is None else min(item["slack"], slack)
    table = [
        {
            "source": name,
            "lead": lead,
            "n": ", ".join(f"previous_day{n}: {count}" for n, count in sorted(item["n"].items())) or "—",
            "slack": f"{item['slack']:.1f}" if item["slack"] is not None else "—",
            "naive": f"{item['naive']} из {item['issues']}",
            "none": item["none"],
        }
        for (name, lead), item in sorted(stats.items())
    ]
    return table, problems, checked


# 3. Признаки модели


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _mentions_measure(node: ast.AST) -> bool:
    return any(isinstance(child, ast.Constant) and child.value in MEASURES for child in ast.walk(node))


def _feature_lists(tree: ast.AST) -> list[tuple[int, list[str]]]:
    """Литералы ``FEATURES = [...]`` модуля."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "FEATURES" for target in node.targets):
            if isinstance(node.value, ast.List | ast.Tuple):
                found.append((node.lineno, [item.value for item in node.value.elts if isinstance(item, ast.Constant)]))
    return found


def _scada_feature_inputs(tree: ast.AST, source: str, feature_names: set[str]) -> list[tuple[int, str]]:
    """Где признаки строятся из колонок SCADA: вызов ``*features(...)`` или словарь признаков со значением из SCADA."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _call_name(node).endswith("features"):
            if any(_mentions_measure(arg) for arg in (*node.args, *(keyword.value for keyword in node.keywords))):
                hits.append((node.lineno, ast.get_source_segment(source, node) or _call_name(node)))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value in feature_names and _mentions_measure(value):
                    hits.append((key.lineno, f"{ast.get_source_segment(source, key)}: {ast.get_source_segment(source, value)}"))
    return sorted(set(hits))


def check_features(
    repo: Path = REPO,
    package: str = INFERENCE_PACKAGE,
    feature_modules: Sequence[str] = FEATURE_MODULES,
    training_dir: str = TRAINING_DIR,
) -> Section:
    """Признаки модели на инференсе не содержат значений SCADA и лагов факта.

    Утечка — если пакет сервиса ``ml_service`` читает SCADA или среди признаков есть
    мощность, лаги и окна по факту: в феврале факта нет. Обучение на измеренных
    ветре и температуре SCADA утечкой будущего не считается, на инференсе эти признаки
    строятся из прогноза погоды, поэтому оно попадает в замечания.
    """
    section = Section("5. Признаки модели")
    feature_names: set[str] = set()
    files = sorted(path for path in (repo / package).rglob("*.py") if "__pycache__" not in path.parts)
    feature_paths = {(repo / module).resolve() for module in feature_modules}
    lists = 0
    for path in files:
        rel = _rel(path, repo)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for lineno, module in _imports(tree):
            if module.startswith(SCADA_MODULE) or module.rsplit(".", 1)[-1] in SCADA_LOADERS:
                section.problems.append(f"`{rel}:{lineno}`: сервис модели импортирует загрузчик SCADA `{module}`")
        for lineno, value in _code_tokens(tree):
            if any(marker in value for marker in SCADA_MARKERS):
                section.problems.append(f"`{rel}:{lineno}`: сервис модели ссылается на файл или колонку SCADA `{value[:80]}`")
        for lineno, names in _feature_lists(tree):
            lists += 1
            feature_names.update(names)
            for name in names:
                if name in MEASURES or SUSPICIOUS_FEATURE.search(name):
                    section.problems.append(f"`{rel}:{lineno}`: признак `{name}` похож на значение SCADA или лаг факта")
        if path.resolve() in feature_paths:
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in MEASURES:
                    section.problems.append(f"`{rel}:{node.lineno}`: признаки строятся из колонки SCADA `{node.value}`")
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LAG_CALLS:
                    section.problems.append(f"`{rel}:{node.lineno}`: `.{node.func.attr}(...)` в построении признаков, лаги запрещены GOAL.md")
    missing = [module for module in feature_modules if not (repo / module).is_file()]
    if missing or not lists:
        section.problems.append(f"не найдены модули признаков {missing} или список FEATURES: признаки не проверить")

    training = sorted((repo / training_dir).glob("*.py"))
    for path in training:
        text = path.read_text(encoding="utf-8")
        for lineno, snippet in _scada_feature_inputs(ast.parse(text), text, feature_names):
            section.notes.append(f"`{_rel(path, repo)}:{lineno}`: признаки обучения из измерений SCADA, `{' '.join(snippet.split())[:140]}`")
    if section.notes:
        section.notes.insert(
            0,
            "Модели обучаются на измеренных ветре и температуре турбин, а на инференсе получают ветер и температуру "
            "из прогноза погоды. В февраль факт не попадает, поэтому это не утечка, но это train/serve skew: "
            "GOAL.md §1 (#14) и §6 требуют обучать на архивных прогнозах через тот же as-of.",
        )
    section.checked = f"модулей `{package}`: {len(files)}, скриптов `{training_dir}`: {len(training)}"
    section.details.append("Признаки модели: " + ", ".join(f"`{name}`" for name in sorted(feature_names)) + ".")
    section.details.append(
        "`features_from_frame` берет ветер ступицы и температуру из `frame.summary`, то есть только из строк погоды выпуска."
        if not section.problems
        else "Признаки на инференсе содержат значения SCADA или лаги, см. утечки."
    )
    section.problems = sorted(set(section.problems))
    return section


# Отчет


def _table(rows: Sequence[Mapping[str, Any]], columns: Sequence[tuple[str, str]]) -> list[str]:
    lines = ["| " + " | ".join(title for _, title in columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(str(row[key]) for key, _ in columns) + " |" for row in rows]
    return lines


def _issue_section(title: str, check: IssueCheck, scope: str) -> Section:
    section = Section(title, checked=f"выпусков: {check.issues}, из них с погодой {check.with_weather}; {scope}")
    section.details.append(_rows_line(check))
    if check.missing_sources:
        section.details.append(
            "Источник пропущен без прогона на момент выпуска: "
            + ", ".join(f"{name} в {count} выпусках" for name, count in sorted(check.missing_sources.items()))
            + "."
        )
    if check.no_weather:
        section.details.append(
            f"Выпусков без погоды: {len(check.no_weather)}, {_fmt(check.no_weather[0])}…{_fmt(check.no_weather[-1])}. На них "
            "нет ни одного прогона на момент выпуска, и train.py такие дни тоже пропускает."
        )
    section.problems = check.problems
    return section


def render_report(sections: Sequence[Section], extras: Mapping[str, list[str]]) -> str:
    problems = sum(len(section.problems) for section in sections)
    lines = [
        "# Проверка утечек будущего",
        "",
        "Файл генерирует `make leakcheck` (`cd backend && uv run python -m src.forecast.leakcheck`), руками его не правят.",
        "Код выхода 0 — утечек нет, 1 — найдены. Как устроена проверка: докстринг `backend/src/forecast/leakcheck.py`.",
        "",
        f"**Итог: {'утечек не найдено' if not problems else f'найдено утечек: {problems}'}.**",
        "",
        "| Проверка | Что проверено | Результат |",
        "|---|---|---|",
    ]
    lines += [f"| {s.title} | {s.checked} | {'чисто' if s.ok else f'утечек: {len(s.problems)}'} |" for s in sections]
    for section in sections:
        lines += ["", f"## {section.title}", "", f"{section.checked[:1].upper()}{section.checked[1:]}.", ""]
        lines += [f"{detail}\n" for detail in section.details]
        lines += extras.get(section.title, [])
        if section.problems:
            lines += ["", f"**Утечки ({len(section.problems)}):**", ""]
            lines += [f"- {problem}" for problem in section.problems[:50]]
            if len(section.problems) > 50:
                lines.append(f"- и еще {len(section.problems) - 50}")
        if section.notes:
            lines += ["", "**Замечания, код выхода не меняют:**", ""]
            lines += [f"- {note}" for note in section.notes]
    return "\n".join(lines).rstrip() + "\n"


def run(repo: Path = REPO, data_dir: Path | None = None, reports_dir: Path | None = None) -> tuple[list[Section], Path]:
    """Все проверки. Пишет паспорта февраля и отчет в ``reports_dir``, возвращает разделы и путь к отчету."""
    data_dir = Path(data_dir) if data_dir is not None else dataset_config.DATA_DIR
    reports_dir = Path(reports_dir) if reports_dir is not None else dataset_config.REPORTS_DIR
    cache_root = data_dir / NWP_CACHE_DIR
    store = AsOfStore(cache_root)
    feb_issues, train_issues = issue_times(*FEB_ISSUES), issue_times(*TRAIN_ISSUES)
    extras: dict[str, list[str]] = {}

    static = scan_forbidden_apis(repo)

    feb_check = check_issues(store, feb_issues, keep_frames=True)
    feb = _issue_section("2. Выпуски февраля 2026", feb_check, f"{_fmt(feb_issues[0])}…{_fmt(feb_issues[-1])} UTC, часы T+1…T+{HORIZON_H}")
    manifest_rows, manifest_problems = write_manifests(feb_check.frames, reports_dir / "manifests", data_dir)
    feb.problems = [*feb.problems, *manifest_problems]
    if feb_check.with_weather < len(feb_issues):
        feb.problems.append(f"погода есть только у {feb_check.with_weather} из {len(feb_issues)} выпусков февраля")
    feb.details.append(
        f"Паспорта выпусков ({len(manifest_rows)}): `reports/manifests/<дата>.json`, в каждом `max_available_at_utc` не позже выпуска. "
        "Запас — часы от выхода последнего прогона до выпуска. Запас 0 ч допустим: сравнение нестрогое, "
        "прогон, вышедший ровно в момент выпуска, выпуску доступен."
    )
    extras[feb.title] = _table(
        manifest_rows,
        [
            ("issue", "Выпуск, UTC"),
            ("rows", "Строк"),
            ("sources", "Источники"),
            ("missing", "Нет прогона"),
            ("last_run", "Последний прогон"),
            ("latest", "max available_at, UTC"),
            ("slack", "Запас, ч"),
            ("file", "Паспорт"),
        ],
    )

    train_check = check_issues(store, train_issues)
    train = _issue_section("3. Выпуски обучения", train_check, f"{_fmt(train_issues[0])}…{_fmt(train_issues[-1])} UTC, каждый день периода SCADA")
    train.details.insert(0, "Все дни, по которым `ml/training/train.py` строит проверку Б и калибровку P10/P90, целиком, без выборки.")

    label_rows, label_problems, label_notes = check_prev_runs_labels(cache_root)
    boundary_rows, boundary_problems, boundary_checked = check_prev_runs_boundaries(feb_issues, train_issues, feb_check.frames)
    cache_rows = sum(row["rows"] for row in label_rows)
    prev = Section(
        "4. Previous Runs",
        checked=f"строк кэша: {cache_rows}, источников: {len(label_rows)}, пар (выпуск, час) для choose_n: {boundary_checked}",
        problems=[*label_problems, *boundary_problems],
        notes=label_notes,
    )
    prev.details.append(
        "Метка прогона в кэше сверяется с `run_init_for`: метка старше правила занизила бы время публикации. "
        "IFS (`ifs`) грузится из Single Runs, прогон в нем известен точно, его проверяют разделы 2 и 3."
    )
    prev.details.append(
        f"`choose_n` с задержкой из `sources.py`: февраль по всем часам +1…+{HORIZON_H} со сверкой с выбором `AsOfStore`, "
        f"обучение на граничных часах {', '.join(f'+{lead}' for lead in BOUNDARY_LEADS)}. "
        "Последняя колонка показывает, сколько раз previous_day1 вслепую дал бы прогон, вышедший после выпуска."
    )
    extras[prev.title] = [
        *_table(
            label_rows,
            [
                ("source", "Источник"),
                ("rows", "Строк кэша"),
                ("cycle", "Шаг прогонов, ч"),
                ("step", "Шаг данных, ч"),
                ("delay", "Задержка"),
                ("older", "Метка старше правила"),
                ("newer", "Метка новее правила"),
            ],
        ),
        "",
        *_table(
            boundary_rows,
            [
                ("source", "Источник"),
                ("lead", "Час"),
                ("n", "Выбор choose_n"),
                ("slack", "Мин. запас, ч"),
                ("none", "Нет прогона"),
                ("naive", "previous_day1 вслепую — утечка"),
            ],
        ),
    ]

    features = check_features(repo)
    sections = [static, feb, train, prev, features]
    report = render_report(sections, extras)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / "leakcheck.md"
    path.write_bytes(report.encode("utf-8"))
    return sections, path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Проверка утечек будущего в погоде, выпусках и признаках модели")
    parser.add_argument("--data-dir", type=Path, default=None, help="каталог данных, по умолчанию DATA_DIR")
    parser.add_argument("--reports-dir", type=Path, default=None, help="куда писать отчет и паспорта, по умолчанию REPORTS_DIR")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Пропуски источников и отброшенные строки кэша штатны и сводятся в отчет, в консоли они только шум.
    logging.getLogger("src.forecast.weather").setLevel(logging.ERROR)

    sections, report = run(data_dir=args.data_dir, reports_dir=args.reports_dir)
    for section in sections:
        logger.info("%s %s: %s", "OK    " if section.ok else "УТЕЧКА", section.title, section.checked)
        for problem in section.problems[:MAX_EXAMPLES]:
            logger.info("         %s", problem)
        if len(section.problems) > MAX_EXAMPLES:
            logger.info("         и еще %d", len(section.problems) - MAX_EXAMPLES)
    notes = sum(len(section.notes) for section in sections)
    if notes:
        logger.info("Замечаний, не влияющих на результат: %d, см. отчет", notes)
    failed = [section for section in sections if not section.ok]
    logger.info("Отчет: %s. %s", report, "Утечек не найдено." if not failed else f"Утечки в {len(failed)} проверках.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
