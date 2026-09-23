import json

from src.weather_service.export_openapi import OPENAPI_PATH, openapi_json


def test_committed_openapi_matches_app():
    """Если тест упал: ``cd backend && uv run python -m src.weather_service.export_openapi`` и закоммитить файл."""
    assert json.loads(OPENAPI_PATH.read_text(encoding="utf-8")) == json.loads(openapi_json())
