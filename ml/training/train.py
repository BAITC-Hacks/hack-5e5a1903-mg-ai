"""Обучение LightGBM на SCADA, проверка 80/20 и калибровка интервала по архиву прогнозов погоды.

Запуск из папки ml: ``uv run python -m training.train``. Код backend (загрузчик SCADA и
погода на момент прогноза от dev3) импортируется только здесь, в образ сервиса он не попадает.

1. SCADA по турбинам T1 и T2, часы в UTC, часы с флагами очистки не идут в обучение.
2. 80/20 по случайным целым дням: сезоны перемешаны, часы одного дня не делятся.
3. Проверка А: check-дни на измеренном ветре — точность кривой мощности.
4. Проверка Б: выпуски в 02:00 UTC, погода из архива ``data/nwp`` только из прогонов,
   вышедших к моменту выпуска, ``build_frame_from_rows`` → модель. Это ошибка прогноза.
5. Поправки P10/P90 считаются по выпускам train-дней, покрытие проверяется на check-днях.
6. Итоговые бустеры обучаются на всех днях.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

ML = Path(__file__).resolve().parents[1]
REPO = ML.parent
sys.path.insert(0, str(ML / "src"))
sys.path.insert(0, str(REPO / "backend"))

from src.forecast.dataset.scada import load_scada  # noqa: E402
from src.forecast.weather.asof import AsOfStore  # noqa: E402

from ml_service.features import DESCRIPTIONS, FEATURES, build_features  # noqa: E402
from ml_service.frame import build_frame_from_rows  # noqa: E402
from ml_service.predictors.baseline import passport_curve  # noqa: E402
from ml_service.predictors.lgbm import Calibration  # noqa: E402

ARTIFACTS = ML / "artifacts"
SOURCES = ["ifs", "ifs025", "gfs", "icon", "gem"]
SEEDS = [0, 1, 2, 3, 4]
ISSUE_HOUR_UTC = 2
HORIZON = 48
CHECK_SHARE = 0.2
SPLIT_SEED = 42
LEVEL_EDGES = [0.1, 0.3, 0.6, 0.9]
LEAD_EDGE = 24
PARAMS = dict(
    objective="quantile",
    alpha=0.5,
    learning_rate=0.05,
    num_leaves=31,
    min_data_in_leaf=50,
    bagging_fraction=0.8,
    bagging_freq=1,
    verbose=-1,
    deterministic=True,
    force_row_wise=True,
    num_threads=4,
)
ROUNDS = 500
VERSION = "lgbm-scada-2026-01-31-v1"


def load_hours() -> pd.DataFrame:
    scada = load_scada(REPO / "data")
    scada = scada.dropna(subset=["power_norm", "wind_ms", "temp_c"]).copy()
    scada["time_utc"] = pd.to_datetime(scada["time_utc"], utc=True)
    scada["day"] = scada["time_utc"].dt.normalize()
    return scada


def split_days(hours: pd.DataFrame) -> set:
    days = np.array(sorted(hours["day"].unique()))
    rng = np.random.default_rng(SPLIT_SEED)
    return set(rng.choice(days, size=int(CHECK_SHARE * len(days)), replace=False))


def features_of(hours: pd.DataFrame) -> pd.DataFrame:
    parts = [build_features(g["wind_ms"], g["temp_c"], g["time_utc"], turbine) for turbine, g in hours.groupby("turbine", sort=False)]
    index = np.concatenate([g.index.to_numpy() for _, g in hours.groupby("turbine", sort=False)])
    return pd.concat(parts, ignore_index=True).set_axis(index).loc[hours.index]


def fit(hours: pd.DataFrame) -> list[lgb.Booster]:
    clean = hours[hours["flag"].fillna("") == ""]
    x, y = features_of(clean)[FEATURES].to_numpy(dtype=float), clean["power_norm"].to_numpy()
    return [lgb.train({**PARAMS, "seed": seed}, lgb.Dataset(x, y, feature_name=FEATURES), num_boost_round=ROUNDS) for seed in SEEDS]


def p50(models, x: pd.DataFrame) -> np.ndarray:
    return np.clip(np.mean([m.predict(x[FEATURES].to_numpy(dtype=float)) for m in models], axis=0), 0, 1)


def issues_frame(models, hours: pd.DataFrame, days: list) -> pd.DataFrame:
    """Для каждого выпуска: прогноз по погоде, доступной на 02:00 UTC, и факт SCADA."""
    store = AsOfStore(REPO / "data" / "nwp")
    sources = [s for s in SOURCES if (REPO / "data" / "nwp" / s).exists()]
    actual = hours.set_index(["time_utc", "turbine"])["power_norm"]
    rows = []
    for day in days:
        issue = pd.Timestamp(day) + pd.Timedelta(hours=ISSUE_HOUR_UTC)
        valid = pd.date_range(issue + pd.Timedelta(hours=1), periods=HORIZON, freq="h")
        try:
            nwp = store.get_nwp_multi(sources, issue, valid)
            frame = build_frame_from_rows(issue, HORIZON, nwp)
        except Exception:
            continue
        s = frame.summary
        for turbine in ("T1", "T2"):
            x = build_features(s["wind_speed_hub_ms"], s["t2m"], s.index, turbine)
            out = pd.DataFrame(
                {
                    "issue_time_utc": issue,
                    "valid_time_utc": s.index,
                    "lead_h": s["lead_h"].to_numpy(),
                    "turbine": turbine,
                    "p50": p50(models, x),
                    "spread": s["wind_spread_ms"].to_numpy(dtype=float),
                    "wind": s["wind_speed_hub_ms"].to_numpy(),
                }
            )
            out["actual"] = [actual.get((t, turbine), np.nan) for t in s.index]
            out["persistence"] = actual.get((issue, turbine), np.nan)
            rows.append(out)
    return pd.concat(rows, ignore_index=True).dropna(subset=["actual"])


def fit_calibration(train_issues: pd.DataFrame) -> dict:
    spread_edges = list(np.nanquantile(train_issues["spread"], [1 / 3, 2 / 3]))
    cal = Calibration(
        {"level_edges": LEVEL_EDGES, "spread_edges": spread_edges, "lead_edge": LEAD_EDGE, "q10": np.zeros((2, 5, 3)), "q90": np.zeros((2, 5, 3))}
    )
    lead_bin, level, spread_bin = cal.bins(train_issues["p50"].to_numpy(), train_issues["spread"].to_numpy(), train_issues["lead_h"].to_numpy())
    resid = train_issues["actual"].to_numpy() - train_issues["p50"].to_numpy()
    q10, q90 = np.zeros((2, 5, 3)), np.zeros((2, 5, 3))
    for a in range(2):
        for b in range(5):
            for c in range(3):
                m = (lead_bin == a) & (level == b) & (spread_bin == c)
                r = resid[m] if m.sum() >= 30 else resid[(lead_bin == a) & (level == b)]
                q10[a, b, c], q90[a, b, c] = np.quantile(r, 0.1), np.quantile(r, 0.9)
    return {"level_edges": LEVEL_EDGES, "spread_edges": spread_edges, "lead_edge": LEAD_EDGE, "q10": q10.tolist(), "q90": q90.tolist()}


def nmae(e) -> float:
    return round(float(np.mean(np.abs(e)) * 100), 2)


def metrics_json(check: pd.DataFrame, clim) -> dict:
    e = check["p50"] - check["actual"]
    d1, d2 = check["lead_h"] <= 24, check["lead_h"] > 24
    pers = nmae(check["persistence"].fillna(check["p50"]) - check["actual"])
    cover = float(np.mean((check["actual"] >= check["p10"]) & (check["actual"] <= check["p90"])) * 100)
    by_day = [
        {
            "issue_time_utc": t.isoformat(),
            "nmae_pct": nmae(g["p50"] - g["actual"]),
            "bias_pct": round(float((g["p50"] - g["actual"]).mean() * 100), 2),
        }
        for t, g in check.groupby("issue_time_utc")
    ]
    by_lead = [{"lead_h": int(h), "nmae_pct": nmae(g["p50"] - g["actual"])} for h, g in check.groupby("lead_h")]
    climatology = np.array([clim.get((t.month, t.hour), np.nan) for t in check["valid_time_utc"]])
    return {
        "model_version": VERSION,
        "period_start_utc": check["valid_time_utc"].min().isoformat(),
        "period_end_utc": check["valid_time_utc"].max().isoformat(),
        "nmae_d1_pct": nmae(e[d1]),
        "nmae_d2_pct": nmae(e[d2]),
        "nrmse_48_pct": round(float(np.sqrt(np.mean(e**2)) * 100), 2),
        "skill_vs_persistence_pct": round((1 - nmae(e) / pers) * 100, 1),
        "coverage_p10_p90_pct": round(cover, 1),
        "baselines": [
            {"name": "Персистентность (факт в момент выпуска)", "nmae_pct": pers},
            {"name": "Климатология месяц × час", "nmae_pct": nmae(climatology - check["actual"])},
            {"name": "Паспортная кривая GW109 на прогнозе ветра", "nmae_pct": nmae(passport_curve(check["wind"]) - check["actual"])},
        ],
        "by_day": by_day,
        "by_lead": by_lead,
        "by_turbine": [
            {
                "turbine": turbine,
                "nmae_d1_pct": nmae((g["p50"] - g["actual"])[g["lead_h"] <= 24]),
                "nmae_d2_pct": nmae((g["p50"] - g["actual"])[g["lead_h"] > 24]),
                "coverage_p10_p90_pct": round(float(np.mean((g["actual"] >= g["p10"]) & (g["actual"] <= g["p90"])) * 100), 1),
            }
            for turbine, g in check.groupby("turbine")
        ],
        "series": [],
    }


def main() -> None:
    hours = load_hours()
    check_days = split_days(hours)
    in_check = hours["day"].isin(check_days)
    train, check = hours[~in_check], hours[in_check]
    print(f"часов: train {len(train)}, check {len(check)}; дней в check {len(check_days)}")

    models = fit(train)
    xa = features_of(check)
    for turbine in ("T1", "T2"):
        m = (check["turbine"] == turbine).to_numpy()
        print(f"проверка А {turbine}: nMAE {nmae(p50(models, xa[m]) - check['power_norm'].to_numpy()[m])}% (измеренный ветер)")

    all_days = sorted(hours["day"].unique())
    issues = issues_frame(models, hours, all_days)
    issues["day"] = issues["valid_time_utc"].dt.normalize()
    train_issues = issues[~issues["day"].isin(check_days)]
    check_issues = issues[issues["day"].isin(check_days)].copy()
    calibration = fit_calibration(train_issues)
    lo, hi = Calibration(calibration).interval(check_issues["p50"].to_numpy(), check_issues["spread"].to_numpy(), check_issues["lead_h"].to_numpy())
    check_issues["p10"], check_issues["p90"] = np.clip(lo, 0, 1), np.clip(hi, 0, 1)
    clim_src = train[train["turbine"].isin(["T1", "T2"])]
    clim = clim_src.groupby([clim_src["time_utc"].dt.month, clim_src["time_utc"].dt.hour])["power_norm"].mean().to_dict()
    metrics = metrics_json(check_issues, clim)
    for turbine in ("T1", "T2"):
        g = check_issues[check_issues["turbine"] == turbine]
        print(f"проверка Б {turbine}: lead 1–24 {nmae((g.p50 - g.actual)[g.lead_h <= 24])}%, lead 25–48 {nmae((g.p50 - g.actual)[g.lead_h > 24])}%")
    print(
        "проверка Б итого:",
        {k: metrics[k] for k in ("nmae_d1_pct", "nmae_d2_pct", "nrmse_48_pct", "coverage_p10_p90_pct", "skill_vs_persistence_pct")},
    )
    print("базовые линии:", metrics["baselines"])

    final = fit(hours)
    ARTIFACTS.mkdir(exist_ok=True)
    for old in ARTIFACTS.glob("lgbm_p50_s*.txt"):
        old.unlink()
    for seed, model in zip(SEEDS, final, strict=True):
        model.save_model(str(ARTIFACTS / f"lgbm_p50_s{seed}.txt"))
    (ARTIFACTS / "calibration.json").write_text(json.dumps(calibration, indent=2), encoding="utf-8")
    (ARTIFACTS / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    gain = np.mean([m.feature_importance("gain") for m in final], axis=0)
    curve_ws = np.arange(0, 25.5, 0.5)
    curve = p50(final, build_features(curve_ws, np.full(len(curve_ws), 5.0), pd.DatetimeIndex(["2025-01-15 12:00"] * len(curve_ws)), "T1"))
    info = {
        "name": "LightGBM на SCADA, 5 бустеров",
        "version": VERSION,
        "kind": "lightgbm_quantile",
        "quantiles": [0.1, 0.5, 0.9],
        "trained_until_utc": hours["time_utc"].max().isoformat(),
        "training_period_start_utc": hours["time_utc"].min().isoformat(),
        "train_rows": int((hours["flag"].fillna("") == "").sum()),
        "walk_forward": (
            f"80/20 по случайным дням (seed {SPLIT_SEED}). Проверка на {len(check_days)} днях: выпуск 02:00 UTC, "
            "погода из архива на момент выпуска. Итоговые бустеры обучены на всех днях до 31.01.2026."
        ),
        "turbines": ["T1", "T2", "station"],
        "turbines_info": [
            {"id": "T1", "name": "Турбина 1, Goldwind GW109/2500", "lat": 43.645150, "lon": 78.535604, "capacity_mw": 2.5},
            {"id": "T2", "name": "Турбина 2, Goldwind GW109/2500", "lat": 43.643198, "lon": 78.538828, "capacity_mw": 2.5},
            {"id": "station", "name": "ВЭС целиком, среднее T1 и T2", "capacity_mw": 5.0},
        ],
        "capacity_mw": {"T1": 2.5, "T2": 2.5, "station": 5.0},
        "inputs": {
            "sources": [{"name": s, "required": False} for s in SOURCES],
            "variables": ["ws80", "ws100", "ws120", "t2m"],
            "hub_height_m": 80.0,
            "max_horizon_hours": HORIZON,
        },
        "features": [
            {"name": f, "importance": round(float(g / gain.sum()), 4), "description": DESCRIPTIONS[f]} for f, g in zip(FEATURES, gain, strict=True)
        ],
        "power_curve": [{"wind_ms": float(w), "power_norm": round(float(p), 4)} for w, p in zip(curve_ws, curve, strict=True)],
        "notes": "P50 — среднее 5 бустеров LightGBM (квантиль 0,5), обучены на ветре и температуре SCADA. "
        "В бою на вход идет ветер ступицы и температура, усредненные по пришедшим моделям погоды. "
        "P10/P90 — P50 плюс поправки из calibration.json по уровню прогноза, разбросу моделей погоды и заблаговременности.",
    }
    (ARTIFACTS / "model_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"артефакты записаны в {ARTIFACTS}, {datetime.now(UTC):%H:%M:%S} UTC")


if __name__ == "__main__":
    main()
