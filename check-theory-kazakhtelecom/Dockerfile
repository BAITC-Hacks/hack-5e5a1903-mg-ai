# OrgDiff: агент сопоставления организационных документов. Один образ, один процесс.
FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

FROM python:3.12-slim-bookworm
WORKDIR /app
RUN useradd -m -u 1000 appuser && mkdir -p /app/.cache && chown appuser:appuser /app/.cache
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 ORGDIFF_HOST=0.0.0.0 ORGDIFF_PORT=8765
COPY --chown=appuser:appuser orgdiff ./orgdiff
COPY --chown=appuser:appuser app.py run.py test_formats.py synth_test.py make_testdata.py pyproject.toml ./
COPY --chown=appuser:appuser testdata ./testdata
USER appuser
EXPOSE 8765
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8765/health')"
CMD ["python", "app.py"]
