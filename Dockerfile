FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      curl \
      libjpeg62-turbo-dev \
      zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY server.py index.html ./
COPY assets/ assets/

ENV DOCENT_DATA_DIR=/data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:8000/health || exit 1

CMD ["uv", "run", "python3", "server.py"]
