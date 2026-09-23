"""Общие фикстуры. Тесты самодостаточны: контрольный комплект лежит в testdata/ как pdf и xlsx,
ключ модели для большинства тестов не нужен. Тесты с моделью помечены маркером llm
и пропускаются без OPENAI_API_KEY."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
TESTDATA = ROOT / "testdata"


def _load_env():
    for env in (ROOT / ".env", ROOT.parent / ".env"):
        if env.exists() and not os.environ.get("OPENAI_API_KEY"):
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("OPENAI_API_KEY=") and len(line) > 15:
                    os.environ["OPENAI_API_KEY"] = line.split("=", 1)[1].strip()


_load_env()
HAS_KEY = bool(os.environ.get("OPENAI_API_KEY"))


def pytest_collection_modifyitems(config, items):
    skip = pytest.mark.skip(reason="нужен OPENAI_API_KEY")
    for item in items:
        if "llm" in item.keywords and not HAS_KEY:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def before_pdf() -> Path:
    return TESTDATA / "положение_ред8.pdf"


@pytest.fixture(scope="session")
def after_pdf() -> Path:
    return TESTDATA / "положение_ред9.pdf"


@pytest.fixture(scope="session")
def before_xlsx() -> Path:
    return TESTDATA / "структура_ред8.xlsx"


@pytest.fixture(scope="session")
def after_xlsx() -> Path:
    return TESTDATA / "структура_ред9.xlsx"


@pytest.fixture(scope="session")
def clauses_before(before_pdf):
    from orgdiff.load import load_clauses

    return load_clauses(before_pdf, "до")


@pytest.fixture(scope="session")
def clauses_after(after_pdf):
    from orgdiff.load import load_clauses

    return load_clauses(after_pdf, "после")


@pytest.fixture(scope="session")
def control_result(before_pdf, before_xlsx, after_pdf, after_xlsx):
    """Полный прогон контрольного комплекта без модели: детерминированный, около 10 секунд."""
    from orgdiff.pipeline import run_analysis

    return run_analysis([before_pdf, before_xlsx], [after_pdf, after_xlsx], mode="tfidf", verify=False, golden=True, llm_owners=False)
