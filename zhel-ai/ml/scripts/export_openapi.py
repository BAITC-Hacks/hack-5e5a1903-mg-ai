"""Выгружает контракт сервиса в ml/openapi.json, чтобы его можно было читать без запуска сервиса."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ml_service.main import app  # noqa: E402

OPENAPI_FILE = ROOT / "openapi.json"


def render() -> str:
    return json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n"


if __name__ == "__main__":
    OPENAPI_FILE.write_text(render(), encoding="utf-8")
    print(f"Контракт записан в {OPENAPI_FILE}")
