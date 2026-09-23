"""Обучение участника ансамбля MLP и две проверки качества.

Запуск из ml/: ``uv run python training/train_mlp.py`` (обучение и проверка А),
``--check-nwp`` добавляет проверку Б на архиве прогнозов погоды.

Данные: SCADA через загрузчик dev3 ``backend/src/forecast/dataset/scada.py``, одна
строка на час и турбину, часы с флагами очистки в обучение не идут. Цель — доля
номинала 0…1. Признаки ``ws`` и ``t`` при обучении берутся из измерений SCADA,
в бою — из ``frame.summary`` (``wind_speed_hub_ms`` и ``t2m``).

Деление общее для всех участников ансамбля: случайные 20% целых суток идут в проверку,
остальные в обучение, генератор ``np.random.default_rng(42)``.

Проверка А: проверочные сутки, на входе измеренный ветер SCADA.
Проверка Б: проверочные сутки, на входе прогноз погоды, доступный на момент выпуска.
Выпуск каждый день в 02:00 UTC, источники ifs025, gfs, icon, gem через AsOfStore,
горизонт 48 ч, nMAE отдельно для заблаговременности 1–24 и 25–48 ч.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

ML_DIR = Path(__file__).resolve().parents[1]
REPO = ML_DIR.parent
sys.path.insert(0, str(ML_DIR / "src"))
sys.path.insert(0, str(REPO / "backend"))

from src.forecast.dataset.scada import load_scada  # noqa: E402
from src.forecast.weather.asof import AsOfStore, NoRunAvailable  # noqa: E402

from ml_service.errors import MLServiceError  # noqa: E402
from ml_service.frame import build_frame_from_rows  # noqa: E402
from ml_service.members.mlp import FEATURES, MODEL_FILE, MlpMember, build_pipeline, to_matrix  # noqa: E402

logger = logging.getLogger("train_mlp")

TURBINE_CODE = {"T1": 0, "T2": 1}
CHECK_SHARE = 0.2
SPLIT_SEED = 42
NWP_SOURCES = ["ifs025", "gfs", "icon", "gem"]
ISSUE_HOUR_UTC = 2
HORIZON_H = 48
LEAD_BUCKETS = {"lead_1_24": (1, 24), "lead_25_48": (25, 48)}


def calendar_features(valid_time: pd.DatetimeIndex) -> pd.DataFrame:
    angle = 2 * np.pi * valid_time.dayofyear.to_numpy() / 365.25
    return pd.DataFrame({"hour": valid_time.hour.to_numpy(), "doy_sin": np.sin(angle), "doy_cos": np.cos(angle)})


def scada_features(scada: pd.DataFrame) -> pd.DataFrame:
    calendar = calendar_features(pd.DatetimeIndex(scada["time_utc"]))
    features = pd.DataFrame(
        {
            "ws": scada["wind_ms"].to_numpy(),
            "t": scada["temp_c"].to_numpy(),
            "hour": calendar["hour"],
            "doy_sin": calendar["doy_sin"],
            "doy_cos": calendar["doy_cos"],
            "turbine": scada["turbine"].map(TURBINE_CODE).to_numpy(),
        }
    )
    return features[FEATURES]


def check_days(scada: pd.DataFrame) -> set[pd.Timestamp]:
    days = sorted(pd.DatetimeIndex(scada["time_utc"]).normalize().unique())
    rng = np.random.default_rng(SPLIT_SEED)
    return set(rng.choice(days, size=int(CHECK_SHARE * len(days)), replace=False))


def nmae_pct(actual: pd.Series, predicted: pd.Series) -> float:
    return round(float(np.mean(np.abs(actual - predicted))) * 100, 2)


def per_turbine(frame: pd.DataFrame) -> dict[str, dict[str, float]]:
    return {turbine: {"nmae_pct": nmae_pct(part["actual"], part["p50"]), "hours": len(part)} for turbine, part in frame.groupby("turbine")}


def check_measured(member: MlpMember, scada: pd.DataFrame, is_check: pd.Series) -> dict:
    """Проверка А: сутки проверки, признаки из измерений SCADA."""
    frame = scada.loc[is_check].copy()
    frame["p50"] = member.predict_p50(scada_features(frame))
    frame["actual"] = frame["power_norm"].clip(0, 1)
    clean = frame["flag"] == ""
    return {"clean_hours": per_turbine(frame.loc[clean]), "all_hours": per_turbine(frame)}


def nwp_features(summary: pd.DataFrame) -> pd.DataFrame:
    """Признаки боя из frame.summary: по строке на час и турбину, T1 затем T2."""
    calendar = calendar_features(pd.DatetimeIndex(summary.index))
    parts = []
    for turbine, code in TURBINE_CODE.items():
        part = calendar.assign(
            ws=summary["wind_speed_hub_ms"].to_numpy(),
            t=summary["t2m"].to_numpy(),
            turbine=code,
            valid_time_utc=summary.index,
            lead_h=summary["lead_h"].to_numpy(),
            turbine_name=turbine,
        )
        parts.append(part)
    return pd.concat(parts, ignore_index=True)


def check_forecast(member: MlpMember, scada: pd.DataFrame, days: set[pd.Timestamp], nwp_root: Path) -> dict:
    """Проверка Б: сутки проверки, признаки из прогноза погоды на момент выпуска 02:00 UTC."""
    store = AsOfStore(nwp_root)
    first, last = min(days), max(days)
    issues = pd.date_range(first - pd.Timedelta(days=2), last, freq="D", tz="UTC") + pd.Timedelta(hours=ISSUE_HOUR_UTC)
    predictions, skipped = [], 0
    for issue in issues:
        valid_times = pd.date_range(issue + pd.Timedelta(hours=1), periods=HORIZON_H, freq="h")
        if not valid_times.normalize().isin(list(days)).any():
            continue
        try:
            rows = store.get_nwp_multi(NWP_SOURCES, issue, valid_times)
            frame = build_frame_from_rows(issue, HORIZON_H, rows)
        except (NoRunAvailable, MLServiceError) as exc:
            skipped += 1
            logger.debug("Выпуск %s пропущен: %s", issue, exc)
            continue
        features = nwp_features(frame.summary)
        features["p50"] = member.predict_p50(features[FEATURES])
        features["issue_time_utc"] = issue
        predictions.append(features)
    forecast = pd.concat(predictions, ignore_index=True)
    forecast = forecast[forecast["valid_time_utc"].dt.normalize().isin(list(days))]

    actual = scada[["time_utc", "turbine", "power_norm", "flag"]].rename(columns={"time_utc": "valid_time_utc", "turbine": "turbine_name"})
    joined = forecast.merge(actual, on=["valid_time_utc", "turbine_name"], how="inner")
    joined["actual"] = joined["power_norm"].clip(0, 1)
    joined = joined.drop(columns="turbine").rename(columns={"turbine_name": "turbine"})

    used = joined["issue_time_utc"]
    result = {
        "issues": int(used.nunique()),
        "issues_skipped_no_nwp": skipped,
        "first_issue_utc": used.min().isoformat(),
        "last_issue_utc": used.max().isoformat(),
    }
    for name, (low, high) in LEAD_BUCKETS.items():
        bucket = joined[joined["lead_h"].between(low, high)]
        result[name] = {"clean_hours": per_turbine(bucket[bucket["flag"] == ""]), "all_hours": per_turbine(bucket)}
    return result


def predict_latency_ms(member: MlpMember, rows: int = 96, repeats: int = 50) -> float:
    rng = np.random.default_rng(0)
    features = pd.DataFrame(
        {
            "ws": rng.uniform(0, 20, rows),
            "t": rng.uniform(-20, 30, rows),
            "hour": np.arange(rows) % 24,
            "doy_sin": 0.5,
            "doy_cos": 0.5,
            "turbine": np.arange(rows) // 48,
        }
    )[FEATURES]
    member.predict_p50(features)
    timings = []
    for _ in range(repeats):
        start = time.perf_counter()
        member.predict_p50(features)
        timings.append(time.perf_counter() - start)
    return round(float(np.median(timings)) * 1000, 3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts-dir", type=Path, default=ML_DIR / "artifacts")
    parser.add_argument("--nwp-root", type=Path, default=REPO / "data" / "nwp")
    parser.add_argument("--check-nwp", action="store_true", help="добавить проверку Б на архиве прогнозов погоды")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("src.forecast.weather.asof").setLevel(logging.ERROR)

    scada = load_scada()
    days = check_days(scada)
    is_check = pd.DatetimeIndex(scada["time_utc"]).normalize().isin(list(days))
    train = scada.loc[~is_check & (scada["flag"] == "")]
    logger.info("Сутки: %d в проверке. Часов обучения без флагов: %d", len(days), len(train))

    pipeline = build_pipeline()
    started = time.perf_counter()
    pipeline.fit(to_matrix(scada_features(train)), train["power_norm"].clip(0, 1).to_numpy())
    member = MlpMember(pipeline)

    metrics = {
        "model": "mlp",
        "features": FEATURES,
        "split": {"kind": "random_days", "check_share": CHECK_SHARE, "seed": SPLIT_SEED, "check_days": len(days)},
        "train_hours": len(train),
        "train_seconds": round(time.perf_counter() - started, 1),
        "epochs": int(pipeline.named_steps["mlp"].n_iter_),
        "predict_96_rows_ms": predict_latency_ms(member),
        "check_a_measured_wind": check_measured(member, scada, pd.Series(is_check, index=scada.index)),
    }
    if args.check_nwp:
        metrics["check_b_nwp_forecast"] = check_forecast(member, scada, days, args.nwp_root)

    out_dir = args.artifacts_dir / "mlp"
    out_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipeline, out_dir / MODEL_FILE)
    (out_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n")
    logger.info(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
