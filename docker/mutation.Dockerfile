# syntax=docker/dockerfile:1.12

ARG UBUNTU_VERSION=24.04
ARG UBUNTU_IMAGE_DIGEST=sha256:561618e2c15bf2397621dd04f96926663a3b5616c189cf7e38db7e82f5c538ea
ARG CA_CERTIFICATES_VERSION=20260601~24.04.1
ARG ESPEAK_NG_VERSION=1.51+dfsg-12build1
ARG ACCOUNT_TOOLS_PACKAGE_VERSION=1:4.13+dfsg1-4ubuntu3.2
ARG PYTHON_PACKAGE_VERSION=3.12.3-1ubuntu0.15
ARG DEBIAN_FRONTEND=noninteractive
ARG UV_VERSION=0.12.3
ARG UV_IMAGE_DIGEST=sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

FROM ubuntu:${UBUNTU_VERSION}@${UBUNTU_IMAGE_DIGEST} AS builder

ARG CA_CERTIFICATES_VERSION
ARG PYTHON_PACKAGE_VERSION
ARG DEBIAN_FRONTEND

ENV PATH="/opt/corpuskit-mutation/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never

COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "python3.12=${PYTHON_PACKAGE_VERSION}" \
        "python3.12-venv=${PYTHON_PACKAGE_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb

WORKDIR /opt/corpuskit-mutation

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# uv owns provisioning. Remove the unused venv pip installer after creating the
# locked environment so its independently vendored build-only libraries are not
# part of the runnable mutation image. The Ubuntu builder has no system pip package.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups --no-editable \
    && uv pip uninstall --python .venv/bin/python pip

FROM ubuntu:${UBUNTU_VERSION}@${UBUNTU_IMAGE_DIGEST} AS runtime

ARG CA_CERTIFICATES_VERSION
ARG ESPEAK_NG_VERSION
ARG ACCOUNT_TOOLS_PACKAGE_VERSION
ARG PYTHON_PACKAGE_VERSION
ARG DEBIAN_FRONTEND

ENV PATH="/opt/corpuskit-mutation/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        "ca-certificates=${CA_CERTIFICATES_VERSION}" \
        "espeak-ng=${ESPEAK_NG_VERSION}" \
        "passwd=${ACCOUNT_TOOLS_PACKAGE_VERSION}" \
        "python3.12=${PYTHON_PACKAGE_VERSION}" \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*.deb \
    && groupadd --gid 10001 corpuskit-mutation \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin \
        corpuskit-mutation \
    && install --directory --owner=10001 --group=10001 /workspace

COPY --from=builder --chown=10001:10001 \
    /opt/corpuskit-mutation/.venv /opt/corpuskit-mutation/.venv

# Mutmut needs a writable source checkout because it creates ./mutants. CI mounts
# the checkout at this path and retains only explicit JSON/report artifacts.
WORKDIR /workspace

USER 10001:10001

ENTRYPOINT ["mutmut"]
