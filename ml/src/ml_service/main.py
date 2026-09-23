"""ML-сервис прогноза выработки ВЭС. Контракт описан в schemas.py, Swagger открывается на /docs."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ml_service import service
from ml_service.config import get_settings
from ml_service.errors import MLServiceError
from ml_service.predictors import LoadedModel, load_model
from ml_service.schemas import ErrorResponse, HealthResponse, ModelInfo, ModelMetrics, PredictRequest, PredictResponse

logger = logging.getLogger("ml_service")

DESCRIPTION = """
Почасовой вероятностный прогноз выработки двух турбин ВЭС «Шелек» и станции целиком.

**Кто вызывает.** Только backend: берет строки погоды, доступные на момент T, из `GET /nwp`
сервиса погоды и отправляет их как есть в `POST /predict`. Frontend к сервису напрямую не ходит.
Общая схема: `docs/api-contract.md`.

**Единицы.** `p10`, `p50`, `p90` — доля от номинальной мощности (0…1). Номиналы в `capacity_mw`.
Скорости ветра — м/с: у Open-Meteo нужно запрашивать `wind_speed_unit=ms`, по умолчанию там км/ч.

**Ошибки.** Всегда конверт `{"error": {"code", "message", "details"}}`, как в backend.
Клиент ставит таймаут, при таймауте или 5xx агент переходит на запасной прогноз.
"""

ERROR_RESPONSES = {
    422: {"model": ErrorResponse, "description": "Вход не соответствует контракту или нарушает правило «только погода, доступная к T»"},
    500: {"model": ErrorResponse, "description": "Внутренняя ошибка сервиса"},
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # Модель загружается один раз и дальше только читается: между запросами состояния нет.
    app.state.model = load_model(settings.artifacts_dir)
    info = app.state.model.predictor.info
    logger.info("Модель %s (%s) загружена, артефакты: %s", info.version, info.kind, settings.artifacts_dir)
    yield


app = FastAPI(title="HackAlem ML Service", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)


def _model(request: Request) -> LoadedModel:
    return request.app.state.model


@app.get("/health", response_model=HealthResponse, tags=["System"], summary="Жив ли сервис и какая модель загружена")
def health(request: Request) -> HealthResponse:
    model = _model(request)
    info = model.predictor.info
    return HealthResponse(status="ok" if model.trained else "degraded", model_loaded=model.trained, model_version=info.version, model_kind=info.kind)


@app.get(
    "/model-info", response_model=ModelInfo, tags=["Model"], summary="Паспорт модели: входы, признаки с важностью, кривая мощности, окно обучения"
)
def model_info(request: Request) -> ModelInfo:
    return _model(request).predictor.info


@app.get(
    "/metrics",
    response_model=ModelMetrics,
    tags=["Model"],
    summary="Качество на отложенном периоде: nMAE D+1 и D+2, nRMSE, skill, покрытие P10–P90, по дням и по часам горизонта",
    responses={404: {"model": ErrorResponse, "description": "Метрики еще не посчитаны: METRICS_NOT_AVAILABLE"}},
)
def metrics(request: Request) -> ModelMetrics:
    report = _model(request).metrics
    if report is None:
        raise MLServiceError(404, "METRICS_NOT_AVAILABLE", "Метрики модели еще не посчитаны")
    return report


@app.post(
    "/predict",
    response_model=PredictResponse,
    tags=["Forecast"],
    summary="Прогноз P10/P50/P90 на часы T+1 … T+48 по прогнозам погоды, доступным к T",
    responses=ERROR_RESPONSES,
)
def predict(body: PredictRequest, request: Request) -> PredictResponse:
    return service.predict(body, _model(request))


@app.exception_handler(MLServiceError)
async def ml_service_error_handler(request: Request, exc: MLServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "VALIDATION_ERROR", "message": "Запрос не соответствует контракту", "details": {"errors": errors}}},
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Необработанная ошибка на %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "Внутренняя ошибка ML-сервиса", "details": {}}})
