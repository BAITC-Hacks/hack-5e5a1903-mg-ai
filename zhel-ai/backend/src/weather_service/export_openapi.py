"""Выгружает схему OpenAPI сервиса погоды в ``docs/dev3/weather-openapi.json``.

Запуск из папки backend: ``uv run python -m src.weather_service.export_openapi``.
Тест ``tests/weather_service/test_openapi.py`` падает, если закоммиченный файл отстал от кода.
"""

import json
from pathlib import Path

from src.weather_service.main import app

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "docs" / "dev3" / "weather-openapi.json"


def openapi_json() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    OPENAPI_PATH.write_text(openapi_json(), encoding="utf-8", newline="\n")
