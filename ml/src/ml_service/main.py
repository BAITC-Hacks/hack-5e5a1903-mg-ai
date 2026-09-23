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
from ml_service.schemas import BacktestReport, ErrorResponse, HealthResponse, ModelCard, PredictRequest, PredictResponse

logger = logging.getLogger("ml_service")

DESCRIPTION = """
Почасовой вероятностный прогноз выработки двух турбин ВЭС «Шелек» и станции целиком.

**Кто вызывает.** Только backend: агент собирает прогнозы погоды, доступные на момент T,
и отправляет их в `POST /v1/predict`. Frontend к сервису напрямую не ходит.

**Единицы.** `p10`, `p50`, `p90` — доля от номинальной мощности цели (0…1). Номиналы в `capacity_mw`.
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
    card = app.state.model.predictor.card
    logger.info("Модель %s (%s) загружена, артефакты: %s", card.version, card.kind, settings.artifacts_dir)
    yield


app = FastAPI(title="HackAlem ML Service", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)


def _model(request: Request) -> LoadedModel:
    return request.app.state.model


@app.get("/health", response_model=HealthResponse, tags=["System"], summary="Жив ли сервис и какая модель загружена")
def health(request: Request) -> HealthResponse:
    model = _model(request)
    card = model.predictor.card
    return HealthResponse(status="ok" if model.trained else "degraded", model_loaded=model.trained, model_version=card.version, model_kind=card.kind)


@app.get("/v1/model", response_model=ModelCard, tags=["Model"], summary="Паспорт модели: входы, признаки, важность, кривая мощности, метрики")
def model_card(request: Request) -> ModelCard:
    return _model(request).predictor.card


@app.get(
    "/v1/model/backtest",
    response_model=BacktestReport,
    tags=["Model"],
    summary="Проверка модели на отложенном периоде: выпуски, прогноз и факт по часам",
    responses={404: {"model": ErrorResponse, "description": "Отчет еще не собран: BACKTEST_NOT_AVAILABLE"}},
)
def backtest(request: Request) -> BacktestReport:
    report = _model(request).backtest
    if report is None:
        raise MLServiceError(404, "BACKTEST_NOT_AVAILABLE", "Отчет о проверке модели еще не собран")
    return report


@app.post(
    "/v1/predict",
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
