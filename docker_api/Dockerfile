FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TZ=Asia/Shanghai

# Build provenance (populated at build time via --build-arg; surfaced at /health/version)
ARG BUILD_SERVICE=a-stock-data-api
ARG BUILD_IMAGE=
ARG BUILD_PHASE=
ARG BUILD_RELEASE=
ARG BUILD_COMMIT=
ARG BUILD_TIME=
ENV BUILD_SERVICE=${BUILD_SERVICE} \
    BUILD_IMAGE=${BUILD_IMAGE} \
    BUILD_PHASE=${BUILD_PHASE} \
    BUILD_RELEASE=${BUILD_RELEASE} \
    BUILD_COMMIT=${BUILD_COMMIT} \
    BUILD_TIME=${BUILD_TIME}

WORKDIR /app

# Install dependencies first (layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser && \
    mkdir -p /app/data /app/cache && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live')" || exit 1

CMD ["uvicorn", "astock_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
