"""Сервис погоды dev3: прогнозы погоды строго на момент прогноза и история турбин.

Запуск из папки backend: ``uv run uvicorn src.weather_service.main:app --port 8020``.
Swagger на ``/docs``, схема на ``/openapi.json``, выгрузка в ``docs/dev3/weather-openapi.json``.

``src.core`` не импортируется: его настройки требуют переменных Postgres, а сервису база не нужна.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from src.forecast.dataset import config
from src.weather_service import service
from src.weather_service.errors import WeatherServiceError
from src.weather_service.schemas import ErrorResponse, HealthResponse, NwpResponse, RunInfo, ScadaRow, SourceInfo, TurbineName
from src.weather_service.service import WeatherData

logger = logging.getLogger("weather_service")

DESCRIPTION = """
Архивные прогнозы погоды по точке ВЭС «Шелек» в том виде, в каком они были доступны на момент
прогноза, и почасовая история турбин (SCADA).

**Кто вызывает.** Backend dev1 по `WEATHER_SERVICE_URL`. Строки `GET /nwp` уходят в `POST /predict`
ML-сервиса как есть. Frontend к сервису напрямую не ходит. Общая схема: `docs/api-contract.md`.

**Главное правило.** Ни одна строка погоды не опубликована позже `as_of`: прогон считается доступным
в момент `run_init_utc + задержка публикации источника`. Перед отдачей ответа это проверяется еще раз.

**Время.** ISO 8601 в UTC с `Z`: `2026-01-31T02:00:00Z`. Время без пояса дает 422, время с другим
смещением переводится в UTC. Пустое значение — `null`.

**Источники.** `ifs`, `ifs025`, `gfs`, `icon`, `gem` и `ensemble` — среднее `ifs025`, `gfs`, `icon`, `gem`.
Подробности в `GET /sources`.

**Ошибки.** Всегда конверт `{"error": {"code", "message", "details"}}`, как в backend.
"""


def _error(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def _responses(examples: dict[int, dict[str, tuple[str, dict]]]) -> dict[int | str, dict[str, Any]]:
    descriptions = {
        404: "Нет прогона, опубликованного к as_of",
        422: "Неизвестный источник или ошибка в параметрах",
        500: "Сработала проверка утечки будущего или внутренняя ошибка",
        503: "Данные не загружены",
    }
    return {
        status: {
            "model": ErrorResponse,
            "description": descriptions[status],
            "content": {"application/json": {"examples": {name: {"summary": summary, "value": value} for name, (summary, value) in cases.items()}}},
        }
        for status, cases in examples.items()
    }


VALIDATION = ("Ошибка в параметрах", _error("VALIDATION_ERROR", "Окно больше 168 ч", {"from": "2026-01-31T03:00:00Z", "to": "2026-02-10T03:00:00Z"}))
UNKNOWN = (
    "Неизвестный источник",
    _error(
        "UNKNOWN_SOURCE",
        "Неизвестный источник погоды: ecmwf_ifs",
        {"unknown": ["ecmwf_ifs"], "known": ["ifs", "ifs025", "gfs", "icon", "gem", "ensemble"]},
    ),
)
NO_RUN = (
    "Нет прогона",
    _error(
        "NO_RUN_AVAILABLE",
        "На момент as_of нет опубликованного прогона хотя бы для одного из запрошенных часов",
        {"source": "ifs", "as_of_utc": "2024-03-15T02:00:00Z", "missing_hours": 48, "examples": ["2024-03-15T03:00:00Z"]},
    ),
)
LEAKAGE = ("Проверка утечки", _error("LEAKAGE_GUARD", "Ответ остановлен последней проверкой на утечку будущего: …"))
NO_SCADA = ("SCADA не загружена", _error("DATA_UNAVAILABLE", "SCADA не загружена: нет файлов турбин в DATA_DIR"))

NWP_EXAMPLE = {
    "as_of_utc": "2026-01-31T02:00:00Z",
    "from_utc": "2026-01-31T03:00:00Z",
    "to_utc": "2026-02-02T02:00:00Z",
    "sources": ["gfs", "ensemble"],
    "missing": [],
    "rows": [
        {
            "valid_time_utc": "2026-01-31T03:00:00Z",
            "source": "gfs",
            "run_init_utc": "2026-01-30T00:00:00Z",
            "available_at_utc": "2026-01-30T07:00:00Z",
            "lead_h": 27,
            "ws10": None,
            "ws80": 13.87,
            "ws100": 14.63,
            "ws120": 14.93,
            "wd100": 65.0,
            "gust10": 17.2,
            "t2m": -2.3,
            "rh2m": 49.0,
            "psfc": 956.1,
            "ws_spread": None,
            "members": None,
        },
        {
            "valid_time_utc": "2026-01-31T03:00:00Z",
            "source": "ensemble",
            "run_init_utc": "2026-01-30T00:00:00Z",
            "available_at_utc": "2026-01-30T10:00:00Z",
            "lead_h": 27,
            "ws10": None,
            "ws80": 10.75,
            "ws100": None,
            "ws120": None,
            "wd100": 68.0,
            "gust10": 10.17,
            "t2m": -3.8,
            "rh2m": 62.25,
            "psfc": 954.62,
            "ws_spread": 2.83,
            "members": ["gem", "gfs", "icon", "ifs025"],
        },
    ],
}

RUNS_EXAMPLE = [
    {
        "source": "gfs",
        "run_init_utc": "2026-01-30T00:00:00Z",
        "available_at_utc": "2026-01-30T07:00:00Z",
        "status": "used",
        "lead_from_h": 27,
        "lead_to_h": 74,
        "hours_used": 12,
    },
    {
        "source": "gfs",
        "run_init_utc": "2026-01-30T18:00:00Z",
        "available_at_utc": "2026-01-31T01:00:00Z",
        "status": "used",
        "lead_from_h": 24,
        "lead_to_h": 53,
        "hours_used": 12,
    },
    {
        "source": "gfs",
        "run_init_utc": "2026-01-31T00:00:00Z",
        "available_at_utc": "2026-01-31T07:00:00Z",
        "status": "after_as_of",
        "lead_from_h": 24,
        "lead_to_h": 50,
        "hours_used": 0,
    },
]

SCADA_EXAMPLE = [
    {"time_utc": "2026-01-30T20:00:00Z", "turbine": "T1", "power_norm": 0.1867, "wind_ms": 5.73, "temp_c": -1.54, "flag": ""},
    {"time_utc": "2026-01-28T09:00:00Z", "turbine": "T1", "power_norm": 0.01, "wind_ms": 4.43, "temp_c": 0.05, "flag": "icing"},
]

SourceParam = Annotated[
    str,
    Query(description="Один или несколько источников через запятую: ifs, ifs025, gfs, icon, gem, ensemble", examples=["gfs,ensemble"]),
]
AsOfParam = Annotated[
    AwareDatetime, Query(description="Момент прогноза: только прогоны, опубликованные не позже него", examples=["2026-01-31T02:00:00Z"])
]


def create_app(data: WeatherData | None = None, data_dir: Path | None = None) -> FastAPI:
    """Приложение. ``data`` передают тесты, иначе кэш и SCADA читаются из ``DATA_DIR`` при старте."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        if getattr(app.state, "data", None) is None:
            root = data_dir or config.DATA_DIR
            app.state.data = WeatherData.load(root)
            logger.info("Данные загружены из %s: %s", root, service.health(app.state.data))
        yield

    app = FastAPI(title="HackAlem Weather Service", version="1.0.0", description=DESCRIPTION, lifespan=lifespan)
    app.state.data = data

    @app.exception_handler(WeatherServiceError)
    async def weather_error(_: Request, exc: WeatherServiceError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=_error(exc.code, exc.message, exc.details))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
        return JSONResponse(status_code=422, content=_error("VALIDATION_ERROR", "Параметры запроса не прошли проверку", {"errors": problems}))

    @app.exception_handler(Exception)
    async def internal_error(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("Необработанная ошибка: %s", exc)
        return JSONResponse(status_code=500, content=_error("INTERNAL_ERROR", "Внутренняя ошибка сервиса погоды"))

    def loaded(request: Request) -> WeatherData:
        return request.app.state.data

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["System"],
        summary="Жив ли сервис и что загружено",
        description="Всегда 200, пока процесс жив: по нему работает healthcheck. `degraded` значит, что кэш какого-то источника или SCADA пуст.",
    )
    def health(request: Request) -> dict:
        return service.health(loaded(request))

    @app.get(
        "/sources",
        response_model=list[SourceInfo],
        tags=["Weather"],
        summary="Реестр источников погоды",
        description="Тип API, шаг прогонов, задержка публикации и ее основание, высоты ветра, пустые колонки, период архива, "
        "состав ансамбля и точность ветра. Высоты, пустые колонки и период считаются по кэшу.",
    )
    def sources(request: Request) -> list[dict]:
        return service.sources(loaded(request))

    @app.get(
        "/nwp",
        response_model=NwpResponse,
        tags=["Weather"],
        summary="Погода, опубликованная к моменту as_of",
        description="Для каждого часа окна — самый свежий прогон с `available_at_utc <= as_of`, в котором есть ветер хотя бы на одной "
        "высоте и `t2m`. Строка прогона без них пропускается, и на этот час берется прогон старше. По умолчанию окно — горизонт выпуска "
        "`as_of + 1 ч … as_of + 48 ч`, ровно то, что ждет `POST /predict`. `from` и `to` включительно, начало часа, не больше 168 ч.\n\n"
        "Один источник без прогона дает 404 `NO_RUN_AVAILABLE`, частичной таблицы не бывает. Из нескольких источников пропускаются "
        "те, у которых прогона нет, они перечислены в `missing`.\n\n"
        "`ensemble` — среднее `ifs025`, `gfs`, `icon`, `gem`: `ws80` — ветер на высоте ступицы (80 м, без него 100 или 120 м "
        "с показателем сдвига 0,14), `ws_spread` — разброс между моделями. **Строки `ensemble` не отправляйте в `/predict` вместе "
        "со строками участников:** ML-сервис сам усредняет источники, и ансамбль учтется дважды.\n\n"
        "`lead_h` в строке — часы от запуска прогона, а не от выпуска.",
        responses={
            200: {"content": {"application/json": {"example": NWP_EXAMPLE}}},
            **_responses({404: {"no_run": NO_RUN}, 422: {"unknown": UNKNOWN, "validation": VALIDATION}, 500: {"leakage": LEAKAGE}}),
        },
    )
    def nwp(
        request: Request,
        source: SourceParam,
        as_of: AsOfParam,
        from_: Annotated[AwareDatetime | None, Query(alias="from", description="Первый час окна. По умолчанию as_of + 1 ч")] = None,
        to: Annotated[AwareDatetime | None, Query(description="Последний час окна. По умолчанию from + 47 ч")] = None,
    ) -> dict:
        data = loaded(request)
        names = service.parse_sources(source, data)
        moment = service.utc(as_of)
        hours = service.nwp_hours(moment, service.utc(from_) if from_ else None, service.utc(to) if to else None)
        return service.nwp(data, names, moment, hours)

    @app.get(
        "/runs",
        response_model=list[RunInfo],
        tags=["Weather"],
        summary="Прогоны: статус на момент выпуска или события для пересчета",
        description="**С `as_of`** — прогоны, которые покрывают хотя бы один час горизонта `issue_time + 1 ч … + 48 ч`, со статусом "
        "относительно `as_of`: `used` — дал часы горизонта, `stale` — опубликован, но на все его часы есть прогон свежее, "
        "`after_as_of` — опубликован позже (в `WeatherRun` backend это `after_issue`). `issue_time` по умолчанию равен `as_of`, "
        "при пересчете `as_of` позже. Для страницы «Погода».\n\n"
        "**Без `as_of`, с `from` и `to`** — события «прогон стал доступен» в интервале `(from, to]` по времени доступности, "
        "по ним агент пересчитывает выпуск. `status` в этом режиме null. Окно не больше 31 суток.\n\n"
        "`source` по умолчанию — все источники, `ensemble` раскрывается в четыре модели. Порядок — по `available_at_utc`.",
        responses={
            200: {"content": {"application/json": {"example": RUNS_EXAMPLE}}},
            **_responses({422: {"unknown": UNKNOWN, "validation": VALIDATION}, 500: {"leakage": LEAKAGE}}),
        },
    )
    def runs(
        request: Request,
        source: Annotated[str | None, Query(description="Источники через запятую. По умолчанию все", examples=["gfs,icon"])] = None,
        as_of: Annotated[
            AwareDatetime | None, Query(description="Момент, относительно которого считается статус", examples=["2026-01-31T02:00:00Z"])
        ] = None,
        issue_time: Annotated[AwareDatetime | None, Query(description="Момент выпуска, от него считается горизонт. По умолчанию as_of")] = None,
        status: Annotated[str | None, Query(description="Оставить только эти статусы, через запятую", examples=["used,after_as_of"])] = None,
        from_: Annotated[AwareDatetime | None, Query(alias="from", description="Режим событий: начало интервала, не включительно")] = None,
        to: Annotated[AwareDatetime | None, Query(description="Режим событий: конец интервала, включительно")] = None,
    ) -> list[dict]:
        data = loaded(request)
        names = service.parse_sources(source, data, default_all=True)
        statuses = service.parse_statuses(status)
        start, end = (service.utc(from_) if from_ else None), (service.utc(to) if to else None)

        if as_of is None:
            if start is None or end is None:
                raise service.errors.validation_error("Нужен as_of либо оба параметра from и to")
            if statuses is not None or issue_time is not None:
                raise service.errors.validation_error("status и issue_time имеют смысл только вместе с as_of")
            service.check_window(start, end, service.MAX_WINDOW, ("from", "to"))
            return service.run_events(data, names, start, end)

        moment = service.utc(as_of)
        issue = service.utc(issue_time) if issue_time else moment.floor("h")
        service.require_hour("issue_time", issue)
        if moment < issue:
            raise service.errors.validation_error(
                "as_of не может быть раньше issue_time", {"as_of": service.iso(moment), "issue_time": service.iso(issue)}
            )
        result = service.runs_at(data, names, moment, issue)
        if start is not None:
            result = [run for run in result if run["available_at_utc"] > start]
        if end is not None:
            result = [run for run in result if run["available_at_utc"] <= end]
        if statuses is not None:
            result = [run for run in result if run["status"] in statuses]
        return result

    @app.get(
        "/scada",
        response_model=list[ScadaRow],
        tags=["SCADA"],
        summary="Почасовая история турбин",
        description="Интервал `[from, until)`, не больше 31 суток. Время уже переведено в UTC из UTC+6 файлов SCADA. "
        "Данные заканчиваются 31.01.2026 17:00 UTC, для более поздних часов придет пустой список.",
        responses={
            200: {"content": {"application/json": {"example": SCADA_EXAMPLE}}},
            **_responses({422: {"validation": VALIDATION}, 503: {"no_scada": NO_SCADA}}),
        },
    )
    def scada(
        request: Request,
        from_: Annotated[AwareDatetime, Query(alias="from", description="Начало интервала, включительно", examples=["2026-01-01T00:00:00Z"])],
        until: Annotated[AwareDatetime, Query(description="Конец интервала, не включительно", examples=["2026-02-01T00:00:00Z"])],
        turbine: Annotated[TurbineName | None, Query(description="T1 или T2. По умолчанию обе")] = None,
    ) -> list[dict]:
        return service.scada(loaded(request), service.utc(from_), service.utc(until), turbine)

    return app


app = create_app()
