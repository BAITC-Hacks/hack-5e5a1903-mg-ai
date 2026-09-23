"""Прогноз по запросу: проверка входа, модель, калибровка интервала, ответ по контракту."""

import logging
import time

import numpy as np
import pandas as pd

from ml_service.errors import invalid_input
from ml_service.frame import Frame, build_frame
from ml_service.predictors import LoadedModel
from ml_service.schemas import (
    ALL_TARGETS,
    CAPACITY_MW,
    ForecastPoint,
    HourlyInputs,
    ModelCard,
    ModelRef,
    PredictRequest,
    PredictResponse,
    PredictWarning,
)

logger = logging.getLogger(__name__)

_TARGET_ORDER = {target: n for n, target in enumerate(ALL_TARGETS)}


def predict(request: PredictRequest, model: LoadedModel) -> PredictResponse:
    started = time.perf_counter()
    card = model.predictor.card
    frame = build_frame(request)
    warnings = _source_warnings(frame, card, request.horizon_hours)
    if not model.trained:
        warnings.append(PredictWarning(code="BASELINE_MODEL", message="Обученной модели нет, прогноз по паспортной кривой мощности"))

    targets = list(dict.fromkeys(request.options.targets))
    table = _calibrate(model.predictor.predict(frame, targets), request.options.interval_scale)
    table["order"] = table["target"].map(_TARGET_ORDER)
    table = table.sort_values(["valid_time_utc", "order"])
    lead = frame.summary["lead_h"]

    response = PredictResponse(
        request_id=request.request_id,
        issue_time_utc=frame.issue_time.to_pydatetime(),
        model=ModelRef(name=card.name, version=card.version, kind=card.kind, trained_until_utc=card.trained_until_utc),
        unit="capacity_fraction",
        capacity_mw={target: CAPACITY_MW[target] for target in targets},
        forecast=[
            ForecastPoint(
                valid_time_utc=row.valid_time_utc.to_pydatetime(),
                lead_h=int(lead[row.valid_time_utc]),
                target=row.target,
                p10=row.p10,
                p50=row.p50,
                p90=row.p90,
            )
            for row in table.itertuples(index=False)
        ],
        hourly_inputs=[
            HourlyInputs(
                valid_time_utc=time_utc.to_pydatetime(),
                lead_h=int(row.lead_h),
                wind_speed_hub_ms=round(float(row.wind_speed_hub_ms), 2),
                wind_spread_ms=_rounded(row.wind_spread_ms),
                temperature_2m=_rounded(row.temperature_2m),
                sources=row.sources,
            )
            for time_utc, row in frame.summary.iterrows()
        ],
        warnings=warnings,
        degraded=bool(warnings),
        inference_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    logger.info(
        "predict request_id=%s issue=%s rows=%d model=%s degraded=%s ms=%.1f",
        request.request_id,
        frame.issue_time.isoformat(),
        len(request.weather),
        card.version,
        response.degraded,
        response.inference_ms,
    )
    return response


def _source_warnings(frame: Frame, card: ModelCard, horizon: int) -> list[PredictWarning]:
    missing_required = [s.name for s in card.inputs.sources if s.required and frame.hours_by_source.get(s.name, 0) < horizon]
    if missing_required:
        raise invalid_input(
            "MISSING_REQUIRED_SOURCE",
            "Нет обязательной модели погоды на весь горизонт",
            {"sources": missing_required, "hours_by_source": frame.hours_by_source, "horizon_hours": horizon},
        )
    warnings = []
    for spec in card.inputs.sources:
        hours = frame.hours_by_source.get(spec.name, 0)
        if hours == 0:
            warnings.append(PredictWarning(code="SOURCE_MISSING", message=f"Нет данных модели погоды {spec.name}", source=spec.name))
        elif hours < horizon:
            warnings.append(PredictWarning(code="PARTIAL_SOURCE", message=f"{spec.name}: данные на {hours} из {horizon} ч", source=spec.name))
    return warnings


def _calibrate(raw: pd.DataFrame, interval_scale: float) -> pd.DataFrame:
    quantiles = np.sort(raw[["p10", "p50", "p90"]].to_numpy(dtype=float), axis=1)
    if np.isnan(quantiles).any():
        raise RuntimeError("Модель вернула пропуски в квантилях")
    median = quantiles[:, 1:2]
    quantiles = np.clip(median + (quantiles - median) * interval_scale, 0.0, 1.0)
    table = raw[["valid_time_utc", "target"]].copy()
    table[["p10", "p50", "p90"]] = np.round(quantiles, 4)
    return table


def _rounded(value) -> float | None:
    return None if pd.isna(value) else round(float(value), 2)
