"""HTTP-слой прогноза. Логики здесь нет, только вызовы сервиса.

Все пути наружу начинаются с ``/api``: приложение живет с ``root_path="/api"``,
поэтому в роутере этот префикс не пишется.

Маршруты с постоянным путем объявлены раньше ``/{issue_date}``, иначе FastAPI
попытается разобрать слово ``site`` как дату.
"""

from datetime import date

from fastapi import APIRouter, Depends, Query

from src.core.base_schemas import PaginatedResponse
from src.modules.auth.dependencies import get_current_user
from src.modules.forecast import service
from src.modules.forecast.schemas import (
    AgentDecision,
    BacktestMetrics,
    DispatchResponse,
    ForecastResponse,
    IssueSummary,
    ModelInfo,
    SiteInfo,
    WeatherResponse,
)

router = APIRouter(prefix="/forecast", tags=["Forecast"], dependencies=[Depends(get_current_user)])


@router.get("/issues", response_model=PaginatedResponse[IssueSummary])
async def list_issues(page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=100)) -> PaginatedResponse[IssueSummary]:
    """Дни выпуска ретро-симуляции: 31.01.2026 … 27.02.2026."""
    issues = service.list_issues()
    start = (page - 1) * size
    chunk = issues[start : start + size]
    total = len(issues)
    return PaginatedResponse[IssueSummary](
        items=chunk,
        total=total,
        page=page,
        size=size,
        pages=max(1, -(-total // size)),
    )


@router.get("/site", response_model=SiteInfo)
async def site() -> SiteInfo:
    """Паспорт объекта: турбины, координаты, кривая."""
    return service.build_site()


@router.get("/backtest", response_model=BacktestMetrics)
async def backtest() -> BacktestMetrics:
    """Метрики качества прогноза против бейзлайнов."""
    return service.build_backtest()


@router.get("/model", response_model=ModelInfo)
async def model_info() -> ModelInfo:
    """Чем считаем прогноз: модель, признаки, кривая мощности."""
    return service.build_model_info()


@router.get("/{issue_date}", response_model=ForecastResponse)
async def forecast(issue_date: date) -> ForecastResponse:
    """Почасовой прогноз на 48 часов по каждой турбине."""
    return service.build_forecast(issue_date)


@router.get("/{issue_date}/agent-log", response_model=list[AgentDecision])
async def agent_log(issue_date: date) -> list[AgentDecision]:
    """Журнал решений агента по шагам ТЗ."""
    return service.build_agent_log(issue_date)


@router.get("/{issue_date}/weather", response_model=WeatherResponse)
async def weather(issue_date: date) -> WeatherResponse:
    """Какие прогоны были доступны на момент выпуска и что говорят модели."""
    return service.build_weather(issue_date)


@router.get("/{issue_date}/dispatch", response_model=DispatchResponse)
async def dispatch(issue_date: date, risk: float = Query(0.2, ge=0.1, le=0.5)) -> DispatchResponse:
    """Почасовая заявка на сутки D при заданном допустимом риске недовыработки."""
    return service.build_dispatch(issue_date, risk)


@router.post("/{issue_date}/recompute", response_model=ForecastResponse)
async def recompute(issue_date: date) -> ForecastResponse:
    """Пересчитать выпуск.

    Пока заглушка детерминирована и возвращает тот же прогноз. Когда появятся
    сервисы погоды и модели, здесь запускается цикл агента с новым ``as_of``.
    """
    return service.build_forecast(issue_date)
