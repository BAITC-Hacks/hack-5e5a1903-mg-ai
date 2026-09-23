# Stage 1: Builder
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY pyproject.toml uv.lock ./

RUN uv sync --frozen --no-install-project --no-dev

# Stage 2: Final
FROM python:3.12-slim-bookworm

# LightGBM на Linux требует OpenMP-рантайм, в slim-образе его нет.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN useradd -m -u 1000 appuser

COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    ML_ARTIFACTS_DIR=/app/artifacts

COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser artifacts ./artifacts

USER appuser

EXPOSE 8000

CMD ["uvicorn", "ml_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
