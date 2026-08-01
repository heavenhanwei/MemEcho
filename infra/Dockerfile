FROM python:3.12-slim AS base

RUN apt-get update && \
    apt-get install -y --no-install-recommends libsndfile1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY services/gateway/pyproject.toml services/gateway/pyproject.toml
COPY services/gateway/src/ services/gateway/src/

RUN pip install --no-cache-dir "./services/gateway[dev]"

COPY services/gateway/tests/ services/gateway/tests/

ENV PYTHONPATH=/app/services/gateway/src
ENV MEMECHO_PROVIDER=mock
ENV MEMECHO_DEMO_TOKEN=change-me
ENV MEMECHO_DATA_DIR=/tmp/memecho
ENV MEMECHO_PUBLIC_BASE_URL=http://127.0.0.1:8787

EXPOSE 8787

CMD ["uvicorn", "memecho_gateway.main:app", "--host", "0.0.0.0", "--port", "8787"]
