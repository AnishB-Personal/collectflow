# Stage 1: Build virtual environment
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Stage 2: Minimal runtime image
FROM python:3.12-slim AS runner

WORKDIR /app

# Create non-root group and user, ensure /app directory ownership
RUN groupadd -g 10001 collectflow && \
    useradd -u 10001 -g collectflow -s /bin/bash -m collectflow && \
    mkdir -p /app/data && \
    chown -R collectflow:collectflow /app

# Copy virtual environment from builder
COPY --from=builder --chown=collectflow:collectflow /opt/venv /opt/venv

# Configure environment
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy application source and assets
COPY --chown=collectflow:collectflow app/ ./app/
COPY --chown=collectflow:collectflow migrations/ ./migrations/
COPY --chown=collectflow:collectflow static/ ./static/
COPY --chown=collectflow:collectflow scripts/init_production_db.py ./scripts/init_production_db.py
COPY --chown=collectflow:collectflow alembic.ini ./

USER collectflow:collectflow

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health")' || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
