# syntax=docker/dockerfile:1.12

ARG PYTHON_VERSION=3.12.13
ARG PYTHON_IMAGE_DIGEST=sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG UV_VERSION=0.12.3
ARG UV_IMAGE_DIGEST=sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm@${PYTHON_IMAGE_DIGEST} AS builder-base

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

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

FROM python:${PYTHON_VERSION}-slim-bookworm@${PYTHON_IMAGE_DIGEST} AS runtime-base

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
    && apt-get install --yes --no-install-recommends espeak-ng \
    && rm -rf /var/lib/apt/lists/* \
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
