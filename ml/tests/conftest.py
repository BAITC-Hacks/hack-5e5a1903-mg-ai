import pandas as pd
import pytest
from fastapi.testclient import TestClient

ISSUE_TIME = "2026-01-31T02:00:00Z"


def make_request(horizon: int = 48, sources: tuple[str, ...] = ("ecmwf_ifs", "gfs_global"), **overrides) -> dict:
    issue = pd.Timestamp(ISSUE_TIME)
    rows = []
    for lead in range(1, horizon + 1):
        valid_time = (issue + pd.Timedelta(hours=lead)).isoformat()
        for n, source in enumerate(sources):
            rows.append(
                {
                    "valid_time_utc": valid_time,
                    "source": source,
                    "run_init_utc": "2026-01-30T18:00:00Z",
                    "available_at_utc": "2026-01-31T01:30:00Z",
                    "lead_h": lead,
                    "ws100": 4.0 + lead % 12 + 0.5 * n,
                    "wd100": 250.0,
                    "t2m": -5.0 - n,
                }
            )
    body = {"request_id": "test", "issue_time_utc": ISSUE_TIME, "horizon_hours": horizon, "rows": rows}
    body.update(overrides)
    return body


@pytest.fixture
def artifacts_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ML_ARTIFACTS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(artifacts_dir):
    from ml_service.main import app

    with TestClient(app) as test_client:
        yield test_client
