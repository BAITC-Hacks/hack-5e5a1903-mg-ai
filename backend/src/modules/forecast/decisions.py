"""Журнал решений агента.

Каждое решение, которое меняет ход выполнения, попадает сюда вместе с кодом
причины: какой источник взят, почему отвергнут прогон, почему выпуск ушел
на заглушку, почему опубликована новая версия. По журналу видно, что агент
проверил и почему выбрал именно этот путь.

Имена шагов и коды причин общие для всего проекта: их же использует офлайн-цикл
агента. Одна строка журнала — это ``AgentDecision`` из ``schemas.py``, то есть
ровно то, что видит страница «Агент».

Журнал живет один вызов и создается заново на каждый запрос: состояния в памяти
процесса тут нет.
"""

from dataclasses import dataclass, field
from datetime import datetime

from src.modules.forecast.schemas import AgentDecision

STEP_FETCH_WEATHER = "fetch_weather"
STEP_PREPARE = "prepare"
STEP_RUN_MODEL = "run_model"
STEP_FORECAST = "forecast"
STEP_ANALYZE = "analyze"
STEP_RECOMPUTE = "recompute_on_update"

#: Шаги агентного цикла в порядке ТЗ.
STEPS: tuple[str, ...] = (
    STEP_FETCH_WEATHER,
    STEP_PREPARE,
    STEP_RUN_MODEL,
    STEP_FORECAST,
    STEP_ANALYZE,
    STEP_RECOMPUTE,
)

REASON_USE_SOURCE = "USE_SOURCE"
REASON_FALLBACK = "FALLBACK"
REASON_VALIDATE_FAIL = "VALIDATE_FAIL"
REASON_NO_SOURCE = "NO_SOURCE"
REASON_RISK_FLAGS = "RISK_FLAGS"
REASON_PUBLISH_NEW_VERSION = "PUBLISH_NEW_VERSION"
REASON_NO_MATERIAL_CHANGE = "NO_MATERIAL_CHANGE"
#: Строки погоды, опубликованные позже момента выпуска, выброшены как утечка будущего.
REASON_LEAKAGE_DROPPED = "LEAKAGE_DROPPED"

LEVEL_TOOL = "TOOL"
LEVEL_THINK = "THINK"
LEVEL_OK = "OK"
LEVEL_WARN = "WARN"


@dataclass
class DecisionLog:
    """Журнал одного выпуска, включая его пересчеты."""

    issue_time_utc: datetime
    step: str = STEP_FETCH_WEATHER
    rows: list[AgentDecision] = field(default_factory=list)

    def at(self, step: str) -> None:
        """Перейти на следующий шаг цикла.

        Шаг запоминается, поэтому при отказе соседнего сервиса видно,
        на чем именно агент споткнулся.
        """
        self.step = step

    def record(
        self,
        decision: str,
        reason_code: str,
        reason: str,
        *,
        level: str = LEVEL_TOOL,
        as_of: datetime | None = None,
    ) -> AgentDecision:
        entry = AgentDecision(
            as_of_utc=as_of if as_of is not None else self.issue_time_utc,
            step=self.step,
            decision=decision,
            reason_code=reason_code,
            reason=reason,
            level=level,
        )
        self.rows.append(entry)
        return entry

    def reason_codes(self) -> list[str]:
        return [row.reason_code for row in self.rows]
