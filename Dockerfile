FROM python:3.12-slim
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
RUN python -m pip install --no-cache-dir uv==0.13.0 \
    && useradd --system --uid 10001 --create-home procureflow \
    && install -d -o 10001 -g 10001 /data
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY --chown=0:0 procureflow ./procureflow
RUN chmod -R u=rwX,go=rX /app/procureflow
USER 10001
CMD ["/app/.venv/bin/uvicorn", "procureflow.api:app", "--host", "0.0.0.0", "--port", "8000"]
