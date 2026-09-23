"""Границы модуля: источник погоды (dev3) и сервис модели (dev2)."""

from src.modules.forecast.clients.base import UpstreamClient, UpstreamError
from src.modules.forecast.clients.ml import MlClient
from src.modules.forecast.clients.weather import HttpWeatherSource, LocalWeatherSource, WeatherLeakageError, WeatherSource

__all__ = [
    "HttpWeatherSource",
    "LocalWeatherSource",
    "MlClient",
    "UpstreamClient",
    "UpstreamError",
    "WeatherLeakageError",
    "WeatherSource",
]
