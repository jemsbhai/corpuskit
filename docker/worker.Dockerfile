# syntax=docker/dockerfile:1.12

ARG UBUNTU_VERSION=24.04
ARG UBUNTU_IMAGE_DIGEST=sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea
ARG PYTHON_PACKAGE_VERSION=3.12.3-1ubuntu0.15
ARG CA_CERTIFICATES_VERSION=20260601~24.04.1
ARG ESPEAK_NG_VERSION=1.51+dfsg-12build1
ARG ACCOUNT_TOOLS_PACKAGE_VERSION=1:4.13+dfsg1-4ubuntu3.2
ARG DEBIAN_FRONTEND=noninteractive
ARG UV_VERSION=0.12.3
ARG UV_IMAGE_DIGEST=sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

FROM ubuntu:${UBUNTU_VERSION}@${UBUNTU_IMAGE_DIGEST} AS builder-base

ARG CA_CERTIFICATES_VERSION
ARG PYTHON_PACKAGE_VERSION
ARG DEBIAN_FRONTEND

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "python3.12=${PYTHON_PACKAGE_VERSION}" \
        "python3.12-venv=${PYTHON_PACKAGE_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

FROM builder-base AS worker-batch-builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra worker-batch

FROM builder-base AS worker-external-provider-builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra worker-external-provider

FROM builder-base AS worker-gpu-inference-builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra worker-gpu-inference

FROM builder-base AS worker-gpu-training-builder

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra worker-gpu-training

FROM ubuntu:${UBUNTU_VERSION}@${UBUNTU_IMAGE_DIGEST} AS runtime-base

ARG CA_CERTIFICATES_VERSION
ARG ESPEAK_NG_VERSION
ARG ACCOUNT_TOOLS_PACKAGE_VERSION
ARG PYTHON_PACKAGE_VERSION
ARG DEBIAN_FRONTEND

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    HF_HUB_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    CORPUSKIT_RUNTIME_ROLE=worker

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
        /app /app/artifacts /app/data /home/corpuskit/.cache/huggingface \
        /home/corpuskit/.corpusgen

WORKDIR /app

USER 10001:10001

ENTRYPOINT ["corpuskit-worker"]

FROM runtime-base AS worker-batch

COPY --from=worker-batch-builder --chown=10001:10001 /app/.venv /app/.venv

FROM runtime-base AS worker-external-provider

ENV HF_HUB_OFFLINE=0 \
    HF_DATASETS_OFFLINE=0

COPY --from=worker-external-provider-builder --chown=10001:10001 /app/.venv /app/.venv

FROM runtime-base AS worker-gpu-inference

COPY --from=worker-gpu-inference-builder --chown=10001:10001 /app/.venv /app/.venv

FROM runtime-base AS worker-gpu-training

COPY --from=worker-gpu-training-builder --chown=10001:10001 /app/.venv /app/.venv
