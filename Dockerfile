# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

RUN python -m venv /opt/venv

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN /opt/venv/bin/python -m pip install --no-cache-dir .


FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SUNSET_SENTINEL_DATABASE="/data/sunset-sentinel.db" \
    SUNSET_SENTINEL_SAMPLE_DIR="/app/examples" \
    SUNSET_SENTINEL_PORT="8000"

RUN groupadd --gid 10001 sentinel \
    && useradd --uid 10001 --gid sentinel --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin sentinel \
    && mkdir -p /app/examples /data \
    && chown -R 10001:10001 /app /data

COPY --from=builder /opt/venv /opt/venv
COPY --chown=10001:10001 examples /app/examples

WORKDIR /app
USER 10001:10001

EXPOSE 8000
VOLUME ["/data"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; response = urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2); raise SystemExit(0 if response.status == 200 else 1)"]

CMD ["uvicorn", "sunset_sentinel_api.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
