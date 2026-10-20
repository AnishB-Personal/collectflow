# Stage 1: Build virtual environment
ARG PYTHON_VERSION=3.12-alpine@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
FROM python:${PYTHON_VERSION} AS builder

WORKDIR /build

COPY requirements.txt .
RUN apk add --no-cache gcc musl-dev libffi-dev && \
    python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# Stage 2: Minimal runtime image
FROM python:${PYTHON_VERSION} AS runner

WORKDIR /app

ARG VCS_REF=unknown
LABEL org.opencontainers.image.title="collectflow-backend" \
      org.opencontainers.image.description="CollectFlow FastAPI Backend" \
      org.opencontainers.image.source="https://github.com/AnishB-Personal/collectflow" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.vendor="CollectFlow"

# Create non-root group and user, ensure /app directory ownership
RUN addgroup -g 10001 -S collectflow && \
    adduser -u 10001 -S collectflow -G collectflow && \
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

