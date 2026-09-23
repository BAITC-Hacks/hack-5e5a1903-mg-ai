"""Источник погоды для агента: один интерфейс, три реализации.

Основной источник это сервис погоды dev3 по HTTP (``HttpWeatherSource``),
адрес и таймаут берутся из ``WEATHER_SERVICE_URL`` и ``WEATHER_SERVICE_TIMEOUT``.
Тот же самый кэш прогнозов лежит пакетом в нашем образе
(``src/forecast/weather``), поэтому у агента есть запасной ход без сети:
``LocalWeatherSource``. Связывает их ``WeatherGateway``: сервис не ответил —
выпуск строится по пакету в процессе, а причина записывается в журнал решений.

Это ровно то, чего требует ТЗ от шага ``fetch_weather``: отказ основного
источника переключает агента на запасной, а не роняет выпуск.

Все реализации отдают одни и те же ``NwpRow`` и ``RunRow`` и одинаково
превращают отказ в ошибку с кодом:

- нет прогона на момент выпуска — ``WEATHER_NO_RUN``, штатный сценарий,
  агент уходит на следующий источник и помечает выпуск как ``degraded``;
- утечка будущего — ``WEATHER_LEAKAGE``, выпуск не строится вовсе;
- сервис не ответил — ``WEATHER_TIMEOUT`` или ``WEATHER_UNAVAILABLE``;
- ответ не по контракту — ``WEATHER_BAD_RESPONSE``.
"""

import logging
import math
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

import httpx

from src.core.config import settings
from src.core.exceptions import BusinessError
from src.modules.forecast.clients.base import UpstreamClient, UpstreamError, iso_utc
from src.modules.forecast.clients.schemas import NwpRow, RunRow

logger = logging.getLogger(__name__)

SERVICE = "WEATHER"
TITLE = "Источник погоды"

CODE_NO_RUN = f"{SERVICE}_NO_RUN"
CODE_LEAKAGE = f"{SERVICE}_LEAKAGE"
CODE_BAD_RESPONSE = f"{SERVICE}_BAD_RESPONSE"
#: Код сервиса dev3 для штатного «прогона нет»: у нас это ``WEATHER_NO_RUN``.
UPSTREAM_NO_RUN = "NO_RUN_AVAILABLE"
CODE_UNAVAILABLE = f"{SERVICE}_UNAVAILABLE"


class WeatherLeakageError(BusinessError):
    """Погода, опубликованная позже момента выпуска.

    Это не отказ соседа, а нарушение главного ограничения кейса, поэтому такой
    выпуск не строится ни живьем, ни заглушкой: молча подменить его демонстрацией
    значило бы спрятать утечку будущего. Отдельный тип, не ``UpstreamError``,
    именно затем, чтобы цикл агента его не проглотил.
    """

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(status_code=502, code=CODE_LEAKAGE, message=message, details=details)


def no_run_error(source: str, as_of: datetime, reason: str) -> UpstreamError:
    """Нет прогона на момент выпуска: штатная причина уйти на запасной источник."""
    return UpstreamError(
        status_code=503,
        code=CODE_NO_RUN,
        message=f"{TITLE}: нет прогона {source} на момент {iso_utc(as_of)}",
        details={"source": source, "reason": reason},
    )


class WeatherSource(Protocol):
    """Что агенту нужно от погоды. Обе реализации дают ровно это."""

    kind: str

    async def nwp(self, *, source: str, as_of: datetime, valid_times: Sequence[datetime]) -> list[NwpRow]:
        """Прогноз на указанные часы, доступный на момент ``as_of``."""
        ...

    async def runs(self, *, time_from: datetime, time_to: datetime, sources: Sequence[str] | None = None) -> list[RunRow]:
        """Прогоны, ставшие доступными в интервале ``(time_from, time_to]``."""
        ...


class LocalWeatherSource:
    """Погода dev3 из того же процесса: ``AsOfStore`` поверх закоммиченного кэша.

    Хранилище создается на экземпляр источника, а экземпляр живет один запрос,
    поэтому между запросами в памяти процесса ничего не остается и реплики
    бэкенда взаимозаменяемы. Внутри одного выпуска кэш читается один раз:
    агент спрашивает несколько источников и делает пересчет, а чтение файла
    занимает доли секунды и повторять его незачем.
    """

    kind = "local"

    def __init__(self, cache_root: Path | str | None = None) -> None:
        self._cache_root = cache_root
        self._cached_store = None

    def _store(self):
        # Импорт внутри метода: пакет dev3 тянет pandas и numpy, а модуль
        # импортируется при старте приложения ради схем и ошибок.
        from src.forecast.weather.asof import AsOfStore
        from src.forecast.weather.fetch_prev_runs import nwp_dir

        if self._cached_store is None:
            self._cached_store = AsOfStore(self._cache_root if self._cache_root is not None else nwp_dir())
        return self._cached_store

    async def nwp(self, *, source: str, as_of: datetime, valid_times: Sequence[datetime]) -> list[NwpRow]:
        from src.forecast.weather.asof import LeakageError, NoRunAvailable

        try:
            frame = self._store().get_nwp(source, as_of, list(valid_times))
        except NoRunAvailable as error:
            raise no_run_error(source, as_of, str(error)) from error
        except LeakageError as error:
            logger.error("Погода %s содержит утечку будущего на %s: %s", source, iso_utc(as_of), error)
            raise WeatherLeakageError(f"{TITLE}: {error}", {"source": source, "as_of": iso_utc(as_of)}) from error
        except (OSError, ValueError) as error:
            raise UpstreamError(
                status_code=503,
                code=CODE_UNAVAILABLE,
                message=f"{TITLE}: кэш прогнозов {source} не прочитан",
                details={"source": source, "reason": str(error)},
            ) from error

        return _rows_from_frame(frame, source)

    async def runs(self, *, time_from: datetime, time_to: datetime, sources: Sequence[str] | None = None) -> list[RunRow]:
        try:
            events = self._store().run_events(time_from, time_to, sources=list(sources) if sources else None)
        except (OSError, ValueError) as error:
            raise UpstreamError(
                status_code=503,
                code=CODE_UNAVAILABLE,
                message=f"{TITLE}: список прогонов не прочитан",
                details={"reason": str(error)},
            ) from error
        return [
            RunRow(source=event.source, run_init_utc=event.run_init_utc.to_pydatetime(), available_at_utc=event.available_at_utc.to_pydatetime())
            for event in events
        ]


def _plain(value):
    """Пропуск в таблице это ``None``, а не ``NaN``: схема должна видеть пропуск."""
    return None if isinstance(value, float) and math.isnan(value) else value


def _rows_from_frame(frame, source: str) -> list[NwpRow]:
    """Таблица ``AsOfStore`` в строки контракта. Пропуски остаются пропусками."""
    rows: list[NwpRow] = []
    skipped = 0
    for record in frame.to_dict("records"):
        clean = {key: _plain(value) for key, value in record.items()}
        try:
            rows.append(NwpRow.model_validate(clean))
        except ValueError:
            # Час без ветра ни на одной высоте бесполезен: считаем, что его нет.
            skipped += 1
    if skipped:
        logger.warning("Источник %s: %d часов без скорости ветра пропущены", source, skipped)
    if not rows:
        raise UpstreamError(
            status_code=502,
            code=CODE_BAD_RESPONSE,
            message=f"{TITLE}: в прогоне {source} нет ни одного часа со скоростью ветра",
            details={"source": source},
        )
    return rows


class HttpWeatherSource(UpstreamClient):
    """Сервис погоды dev3 по HTTP: основной источник.

    Адрес и таймаут приходят из ``WEATHER_SERVICE_URL`` и ``WEATHER_SERVICE_TIMEOUT``.
    Строки ``/nwp`` уходят в ML-сервис как есть, без переименования полей:
    так договорились на границе, и так меньше мест, где можно ошибиться.
    Ответ 404 ``NO_RUN_AVAILABLE`` это штатная ситуация, а не сбой, поэтому
    он превращается в ``WEATHER_NO_RUN``, и агент берет следующий источник.
    """

    kind = "http"
    service = SERVICE
    title = TITLE

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            base_url=base_url if base_url is not None else settings.WEATHER_SERVICE_URL,
            timeout=timeout if timeout is not None else settings.WEATHER_SERVICE_TIMEOUT,
            transport=transport,
        )

    async def nwp(self, *, source: str, as_of: datetime, valid_times: Sequence[datetime]) -> list[NwpRow]:
        moments = sorted(valid_times)
        try:
            rows = await self.fetch_rows(
                NwpRow,
                "GET",
                "/nwp",
                params={
                    "source": source,
                    "as_of": iso_utc(as_of),
                    "from": iso_utc(moments[0]),
                    "to": iso_utc(moments[-1]),
                },
            )
        except UpstreamError as error:
            if error.details.get("upstream_code") == UPSTREAM_NO_RUN:
                raise no_run_error(source, as_of, "сервис погоды сообщил, что прогона нет") from error
            raise

        wanted = set(moments)
        chosen = {row.valid_time_utc: row for row in rows if row.valid_time_utc in wanted and row.available_at_utc <= as_of}
        missing = wanted - set(chosen)
        if missing:
            raise no_run_error(source, as_of, f"сервис не отдал {len(missing)} часов горизонта")
        return [chosen[moment] for moment in moments]

    async def runs(self, *, time_from: datetime, time_to: datetime, sources: Sequence[str] | None = None) -> list[RunRow]:
        rows = await self.fetch_rows(
            RunRow,
            "GET",
            "/runs",
            params={"as_of": iso_utc(time_from), "from": iso_utc(time_from), "to": iso_utc(time_to)},
        )
        known = set(sources) if sources else None
        return [row for row in rows if time_from < row.available_at_utc <= time_to and (known is None or row.source in known)]


class WeatherGateway:
    """Сервис погоды с запасным ходом на пакет в процессе.

    Правило простое. Сервис ответил, пусть даже «прогона нет» — работаем с его
    ответом. Сервис не ответил совсем или ответил не по контракту — берем тот же
    кэш прогнозов из нашего образа, чтобы выпуск состоялся. Каждое такое
    переключение возвращается в ``notes`` и попадает в журнал решений агента:
    молчаливой подмены источника быть не должно.
    """

    kind = "gateway"

    def __init__(self, primary: WeatherSource | None = None, spare: WeatherSource | None = None) -> None:
        self.primary = primary if primary is not None else HttpWeatherSource()
        self.spare = spare if spare is not None else LocalWeatherSource()
        self.used_spare = False
        self.notes: list[tuple[str, str]] = []

    async def nwp(self, *, source: str, as_of: datetime, valid_times: Sequence[datetime]) -> list[NwpRow]:
        if self.used_spare:
            return await self.spare.nwp(source=source, as_of=as_of, valid_times=valid_times)
        try:
            return await self.primary.nwp(source=source, as_of=as_of, valid_times=valid_times)
        except UpstreamError as error:
            self._switch(error)
            return await self.spare.nwp(source=source, as_of=as_of, valid_times=valid_times)

    async def runs(self, *, time_from: datetime, time_to: datetime, sources: Sequence[str] | None = None) -> list[RunRow]:
        if self.used_spare:
            return await self.spare.runs(time_from=time_from, time_to=time_to, sources=sources)
        try:
            return await self.primary.runs(time_from=time_from, time_to=time_to, sources=sources)
        except UpstreamError as error:
            self._switch(error)
            return await self.spare.runs(time_from=time_from, time_to=time_to, sources=sources)

    def drain_notes(self) -> list[tuple[str, str]]:
        """Забрать записи о переключениях: дальше они уходят в журнал решений."""
        notes, self.notes = self.notes, []
        return notes

    def _switch(self, error: UpstreamError) -> None:
        """Штатное «прогона нет» источник не меняет, отказ сервиса меняет."""
        if error.code == CODE_NO_RUN:
            raise error
        self.used_spare = True
        message = f"сервис погоды не ответил ({error.message}), беру кэш прогнозов из образа"
        logger.warning(message)
        self.notes.append((error.code, message))
