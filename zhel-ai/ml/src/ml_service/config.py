import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARTIFACTS_DIR = Path(__file__).resolve().parents[2] / "artifacts"


@dataclass(frozen=True)
class Settings:
    artifacts_dir: Path
    log_level: str


def get_settings() -> Settings:
    return Settings(
        artifacts_dir=Path(os.environ.get("ML_ARTIFACTS_DIR") or DEFAULT_ARTIFACTS_DIR),
        log_level=os.environ.get("ML_LOG_LEVEL") or "INFO",
    )
