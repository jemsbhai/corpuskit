# syntax=docker/dockerfile:1.12

ARG UBUNTU_VERSION=24.04
ARG UBUNTU_IMAGE_DIGEST=sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea
ARG CA_CERTIFICATES_VERSION=20260601~24.04.1
ARG ESPEAK_NG_VERSION=1.51+dfsg-12build1
ARG ACCOUNT_TOOLS_PACKAGE_VERSION=1:4.13+dfsg1-4ubuntu3.2
ARG PYTHON_PACKAGE_VERSION=3.12.3-1ubuntu0.16
ARG DEBIAN_FRONTEND=noninteractive
ARG UV_VERSION=0.12.3
ARG UV_IMAGE_DIGEST=sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

FROM ubuntu:${UBUNTU_VERSION}@${UBUNTU_IMAGE_DIGEST} AS builder

ARG CA_CERTIFICATES_VERSION
ARG PYTHON_PACKAGE_VERSION
ARG DEBIAN_FRONTEND

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "python3.12=${PYTHON_PACKAGE_VERSION}" \
        "python3.12-venv=${PYTHON_PACKAGE_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra optimization

FROM ubuntu:${UBUNTU_VERSION}@${UBUNTU_IMAGE_DIGEST} AS runtime

ARG CA_CERTIFICATES_VERSION
ARG ESPEAK_NG_VERSION
ARG ACCOUNT_TOOLS_PACKAGE_VERSION
ARG PYTHON_PACKAGE_VERSION
ARG DEBIAN_FRONTEND

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
    && apt-get install --yes --no-install-recommends \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "espeak-ng=${ESPEAK_NG_VERSION}" \
        "passwd=${ACCOUNT_TOOLS_PACKAGE_VERSION}" \
        "python3.12=${PYTHON_PACKAGE_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
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
