from pathlib import Path

from scripts.export_openapi import OPENAPI_FILE, render


def test_committed_openapi_matches_the_code():
    assert Path(OPENAPI_FILE).read_text(encoding="utf-8") == render(), "Контракт изменился: выполните make ml-openapi и закоммитьте ml/openapi.json"
