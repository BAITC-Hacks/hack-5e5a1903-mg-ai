"""Обучение участника ансамбля на ExtraTreesRegressor.

Запуск из папки ``ml/``: ``uv run python training/train_extratrees.py``.

1. История SCADA через загрузчик dev3 (``backend/src/forecast/dataset/scada.py``),
   в обучение и проверку идут только часы без флагов очистки.
2. Дни делятся случайно: 20 % дней уходят в проверку (seed 42), остальные в обучение.
   Деление общее для всех участников ансамбля.
3. Модель учится на измеренном ветре и температуре SCADA.
4. Проверка А: nMAE на проверочных днях, на входе измеренный ветер.
5. Проверка Б: те же дни, на входе прогноз погоды из архива ``data/nwp`` на момент выпуска.
   Выпуск каждый день в 02:00 UTC, погода через ``AsOfStore``, признаки из ``frame.summary``,
   как в работающем сервисе. nMAE по заблаговременности 1–24 и 25–48 ч.
6. Модель и метрики пишутся в ``ml/artifacts/extratrees/``.

Признаки строятся здесь так же, как в общем ``features.py`` ансамбля: ws, t, час UTC,
sin/cos дня года и номер турбины.
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
import sklearn
from sklearn.ensemble import ExtraTreesRegressor

ML_DIR = Path(__file__).resolve().parents[1]
REPO = ML_DIR.parent
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(ML_DIR / "src"))

from src.forecast.dataset.scada import load_scada  # noqa: E402
from src.forecast.weather.asof import AsOfStore, NoRunAvailable  # noqa: E402

from ml_service.errors import MLServiceError  # noqa: E402
from ml_service.frame import build_frame_from_rows  # noqa: E402
from ml_service.members.extratrees import FEATURES, MEMBER_DIR, METRICS_FILE, MODEL_FILE, ExtraTreesMember  # noqa: E402

logger = logging.getLogger("train_extratrees")

TURBINE_CODES = {"T1": 0, "T2": 1}
CHECK_SHARE = 0.2
SEED = 42
ISSUE_HOUR_UTC = 2
HORIZON = 48
NWP_SOURCES = ("ifs025", "gfs", "icon", "gem")
LEAD_BUCKETS = {"1-24": (1, 24), "25-48": (25, 48)}
PARAMS = {"n_estimators": 200, "min_samples_leaf": 30, "max_features": 1.0, "random_state": SEED}


def make_features(time_utc, ws, t, turbine) -> pd.DataFrame:
    """Таблица признаков в порядке ``FEATURES``. ``turbine`` — метки ``T1``/``T2``."""
    time_utc = pd.DatetimeIndex(time_utc)
    angle = 2 * np.pi * time_utc.dayofyear.to_numpy() / 365.25
    features = pd.DataFrame(
        {
            "ws": np.asarray(ws, dtype=float),
            "t": np.asarray(t, dtype=float),
            "hour": time_utc.hour.to_numpy(dtype=float),
            "doy_sin": np.sin(angle),
            "doy_cos": np.cos(angle),
            "turbine": pd.Series(np.asarray(turbine)).map(TURBINE_CODES).to_numpy(dtype=float),
        }
    )
    return features[list(FEATURES)]


def check_days(time_utc: pd.Series) -> set[pd.Timestamp]:
    """Случайные 20 % дней под проверку. Тот же код и seed у всех участников ансамбля."""
    days = sorted(time_utc.dt.normalize().unique())
    rng = np.random.default_rng(SEED)
    return set(rng.choice(days, size=int(CHECK_SHARE * len(days)), replace=False))


def nmae(actual, predicted) -> float:
    """Средняя абсолютная ошибка в процентах от номинала. Мощность уже в долях номинала."""
    return round(float(np.mean(np.abs(np.asarray(actual) - np.asarray(predicted)))) * 100, 2)


def forecast_table(store: AsOfStore, issues: pd.DatetimeIndex) -> pd.DataFrame:
    """Вход модели в каждом выпуске: час, заблаговременность, ветер и температура из ``frame.summary``.

    Выпуск без прогона погоды или без ветра на часть горизонта пропускается.
    """
    parts, skipped = [], 0
    for issue in issues:
        hours = pd.date_range(issue + pd.Timedelta(hours=1), periods=HORIZON, freq="h")
        try:
            rows = store.get_nwp_multi(NWP_SOURCES, issue, hours)
            summary = build_frame_from_rows(issue, HORIZON, rows).summary
        except (NoRunAvailable, MLServiceError) as exc:
            logger.debug("Выпуск %s пропущен: %s", issue, exc)
            skipped += 1
            continue
        parts.append(
            pd.DataFrame(
                {
                    "time_utc": summary.index,
                    "lead_h": summary["lead_h"].to_numpy(),
                    "ws": summary["wind_speed_hub_ms"].to_numpy(),
                    "t": summary["t2m"].to_numpy(),
                }
            )
        )
    logger.info("Выпусков с погодой: %d, пропущено: %d", len(parts), skipped)
    return pd.concat(parts, ignore_index=True)


def check_measured(member: ExtraTreesMember, check: pd.DataFrame) -> dict[str, float]:
    predicted = member.predict_p50(make_features(check["time_utc"], check["wind_ms"], check["temp_c"], check["turbine"]))
    return {turbine: nmae(check.loc[mask, "power_norm"], predicted[mask.to_numpy()]) for turbine, mask in _by_turbine(check)}


def check_forecast(member: ExtraTreesMember, check: pd.DataFrame, forecasts: pd.DataFrame) -> dict[str, dict[str, float]]:
    table = forecasts.merge(check[["time_utc", "turbine", "power_norm"]], on="time_utc")
    table["p50"] = member.predict_p50(make_features(table["time_utc"], table["ws"], table["t"], table["turbine"]))
    result = {}
    for turbine, mask in _by_turbine(table):
        rows = table.loc[mask]
        buckets = {name: rows[rows["lead_h"].between(lo, hi)] for name, (lo, hi) in LEAD_BUCKETS.items()}
        result[turbine] = {name: nmae(part["power_norm"], part["p50"]) for name, part in buckets.items()}
        result[turbine]["hours"] = int(rows["time_utc"].nunique())
    return result


def predict_seconds(member: ExtraTreesMember, rows: int = 2 * HORIZON, repeats: int = 20) -> float:
    """Медианное время ``predict_p50`` на одном выпуске: 48 часов × 2 турбины."""
    times = pd.date_range("2026-02-01T03:00Z", periods=rows // 2, freq="h").repeat(2)
    features = make_features(times, np.linspace(0, 20, rows), np.full(rows, -5.0), ["T1", "T2"] * (rows // 2))
    member.predict_p50(features)
    spent = []
    for _ in range(repeats):
        start = time.perf_counter()
        member.predict_p50(features)
        spent.append(time.perf_counter() - start)
    return round(float(np.median(spent)), 4)


def _by_turbine(table: pd.DataFrame):
    return ((turbine, table["turbine"] == turbine) for turbine in TURBINE_CODES)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts-dir", type=Path, default=ML_DIR / "artifacts")
    parser.add_argument("--nwp-dir", type=Path, default=REPO / "data" / "nwp")
    parser.add_argument("--skip-forecast-check", action="store_true", help="Без проверки Б на прогнозе погоды")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # Архив погоды начинается в феврале 2024: на каждый более ранний выпуск AsOfStore пишет предупреждение.
    logging.getLogger("src.forecast.weather.asof").setLevel(logging.ERROR)

    history = load_scada()
    clean = history[history["flag"] == ""].reset_index(drop=True)
    in_check = clean["time_utc"].dt.normalize().isin(check_days(history["time_utc"]))
    train, check = clean[~in_check], clean[in_check]
    logger.info("Обучение: %d ч, проверка: %d ч", len(train), len(check))

    model = ExtraTreesRegressor(**PARAMS, n_jobs=-1)
    model.fit(make_features(train["time_utc"], train["wind_ms"], train["temp_c"], train["turbine"]), train["power_norm"])
    # Выпуск маленький, параллельность на 96 строках только тратит время.
    model.set_params(n_jobs=1)
    member = ExtraTreesMember(model)

    metrics = {
        "model": "ExtraTreesRegressor",
        "sklearn_version": sklearn.__version__,
        "params": PARAMS,
        "features": list(FEATURES),
        "split": {"kind": "random_days", "check_share": CHECK_SHARE, "seed": SEED, "check_days": int(check["time_utc"].dt.normalize().nunique())},
        "train_hours": len(train),
        "check_hours": len(check),
        "check_measured_nmae_pct": check_measured(member, check),
        "predict_seconds_96_rows": predict_seconds(member),
    }
    logger.info("Проверка А, nMAE %%: %s", metrics["check_measured_nmae_pct"])

    if not args.skip_forecast_check:
        span = check["time_utc"].dt.normalize()
        issues = pd.date_range(span.min() - pd.Timedelta(days=2), span.max(), freq="D") + pd.Timedelta(hours=ISSUE_HOUR_UTC)
        forecasts = forecast_table(AsOfStore(args.nwp_dir), issues)
        metrics["issue_hour_utc"] = ISSUE_HOUR_UTC
        metrics["nwp_sources"] = list(NWP_SOURCES)
        metrics["check_forecast_nmae_pct"] = check_forecast(member, check, forecasts)
        logger.info("Проверка Б, nMAE %%: %s", metrics["check_forecast_nmae_pct"])

    out = args.artifacts_dir / MEMBER_DIR
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, out / MODEL_FILE, compress=3)
    (out / METRICS_FILE).write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Модель: %s (%.1f МБ)", out / MODEL_FILE, (out / MODEL_FILE).stat().st_size / 2**20)


if __name__ == "__main__":
    main()
