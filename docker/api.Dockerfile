# syntax=docker/dockerfile:1.12

ARG PYTHON_VERSION=3.12.13
ARG PYTHON_IMAGE_DIGEST=sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG UV_VERSION=0.12.3
ARG UV_IMAGE_DIGEST=sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm@${PYTHON_IMAGE_DIGEST} AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra optimization

FROM python:${PYTHON_VERSION}-slim-bookworm@${PYTHON_IMAGE_DIGEST} AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    CORPUSKIT_ENVIRONMENT=development \
    CORPUSKIT_RUNTIME_ROLE=api \
    CORPUSKIT_LOG_LEVEL=INFO \
    CORPUSKIT_DATABASE_URL=sqlite+aiosqlite:////app/data/corpuskit.db \
    CORPUSKIT_ARTIFACT_ROOT=/app/artifacts \
    CORPUSKIT_JOB_BACKEND=inline

RUN apt-get update \
    && apt-get install --yes --no-install-recommends espeak-ng \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 corpuskit \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin corpuskit \
    && install --directory --owner=10001 --group=10001 \
        /app /app/data /app/artifacts /home/corpuskit/.corpusgen

WORKDIR /app

COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=2).read()"]

ENTRYPOINT ["corpuskit-api"]
