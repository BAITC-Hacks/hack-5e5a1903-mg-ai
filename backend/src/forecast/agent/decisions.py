"""Журнал решений агента.

Каждое решение, которое меняет ход выполнения, попадает сюда с причиной.
Записи пишутся в ``outputs/agent_log.jsonl`` и повторяются в паспорте выпуска,
поэтому по журналу видно, что агент проверил и почему выбрал именно этот путь.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

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


def _plain(value: Any) -> Any:
    """Значение, которое переживет ``json.dumps`` без своего сериализатора."""
    if isinstance(value, pd.Timestamp | datetime):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, pd.Timedelta):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return [_plain(v) for v in value]
    if isinstance(value, float | int | bool | str) or value is None:
        return value
    return str(value)


@dataclass(frozen=True)
class Decision:
    """Одна строка журнала."""

    issue_time_utc: str
    as_of_utc: str
    step: str
    decision: str
    reason_code: str
    reason: str
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_record(self) -> dict[str, Any]:
        return {
            "issue_time_utc": self.issue_time_utc,
            "as_of_utc": self.as_of_utc,
            "step": self.step,
            "decision": self.decision,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "inputs": self.inputs,
        }


class DecisionLog:
    """Журнал одного выпуска, включая все пересчеты этого дня."""

    def __init__(self, issue_time_utc: datetime) -> None:
        self.issue_time_utc = pd.Timestamp(issue_time_utc).isoformat()
        self._decisions: list[Decision] = []

    def record(
        self,
        step: str,
        decision: str,
        reason_code: str,
        reason: str,
        *,
        as_of: datetime | None = None,
        **inputs: Any,
    ) -> Decision:
        entry = Decision(
            issue_time_utc=self.issue_time_utc,
            as_of_utc=pd.Timestamp(as_of).isoformat() if as_of is not None else self.issue_time_utc,
            step=step,
            decision=decision,
            reason_code=reason_code,
            reason=reason,
            inputs={key: _plain(value) for key, value in inputs.items()},
        )
        self._decisions.append(entry)
        return entry

    @property
    def decisions(self) -> list[Decision]:
        return list(self._decisions)

    @property
    def records(self) -> list[dict[str, Any]]:
        """Журнал в виде строк для ``agent_log.jsonl``."""
        return [d.as_record() for d in self._decisions]

    def reason_codes(self) -> list[str]:
        return [d.reason_code for d in self._decisions]
