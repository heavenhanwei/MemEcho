# syntax=docker/dockerfile:1.7

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN python -m venv "${VIRTUAL_ENV}" && \
    pip install --upgrade pip setuptools wheel

WORKDIR /build
COPY services/gateway/pyproject.toml services/gateway/pyproject.toml
COPY services/gateway/src/ services/gateway/src/
RUN pip install ./services/gateway

FROM python:3.12-slim AS runtime

ARG APP_UID=10001
ARG APP_GID=10001

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    MEMECHO_DATA_DIR=/var/lib/memecho

RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --system --gid "${APP_GID}" memecho && \
    useradd --system --uid "${APP_UID}" --gid memecho --home-dir /nonexistent \
      --shell /usr/sbin/nologin memecho && \
    install -d -o memecho -g memecho -m 0750 /var/lib/memecho

COPY --from=builder /opt/venv /opt/venv
COPY --chmod=0555 infra/docker-entrypoint.sh /usr/local/bin/memecho-entrypoint

WORKDIR /app
USER memecho:memecho

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/v1/health', timeout=2).read()"]

ENTRYPOINT ["/usr/local/bin/memecho-entrypoint"]
CMD ["--host", "0.0.0.0", "--port", "8787", "--proxy-headers"]
