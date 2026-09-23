"""Внешние зависимости агента: погода (dev3) и модель (dev2).

Агент не знает, откуда берутся прогнозы погоды и как устроена модель. Всё,
что ему нужно, приходит одним объектом ``Providers``. Благодаря этому шаги
агента тестируются без сети, без кэша прогонов и без обученной модели.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd

from ..contract import RunAvailable


class GetNwp(Protocol):
    def __call__(self, source: str, as_of: datetime, valid_times: pd.DatetimeIndex) -> pd.DataFrame: ...


class RunEvents(Protocol):
    def __call__(self, start: datetime, end: datetime) -> list[RunAvailable]: ...


class BuildManifest(Protocol):
    def __call__(self, issue_time: datetime, nwp: pd.DataFrame) -> dict: ...


class BuildFeatures(Protocol):
    def __call__(self, nwp: pd.DataFrame) -> pd.DataFrame: ...


class Baseline(Protocol):
    def __call__(self, kind: str, nwp: pd.DataFrame) -> pd.DataFrame: ...


@dataclass(frozen=True)
class Providers:
    """Контракт из issue #2, собранный в один объект."""

    get_nwp: GetNwp
    run_events: RunEvents
    build_manifest: BuildManifest
    build_features: BuildFeatures
    baseline: Baseline
