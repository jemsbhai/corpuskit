# syntax=docker/dockerfile:1.12

ARG PYTHON_VERSION=3.12.13
ARG PYTHON_IMAGE_DIGEST=sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
ARG UV_VERSION=0.12.3
ARG UV_IMAGE_DIGEST=sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc

FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv

FROM python:${PYTHON_VERSION}-slim-bookworm@${PYTHON_IMAGE_DIGEST}

ENV PATH="/opt/corpuskit-mutation/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

COPY --from=uv /uv /uvx /usr/local/bin/

RUN apt-get update \
    && apt-get install --yes --no-install-recommends espeak-ng \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 corpuskit-mutation \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin \
        corpuskit-mutation \
    && install --directory --owner=10001 --group=10001 /workspace

WORKDIR /opt/corpuskit-mutation

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# uv owns provisioning. Remove both unused pip installers after creating the locked
# environment so their independently vendored build-only libraries are not part of
# the runnable mutation image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --all-groups --no-editable \
    && uv pip uninstall --python .venv/bin/python pip \
    && uv pip uninstall --system pip

# Mutmut needs a writable source checkout because it creates ./mutants. CI mounts
# the checkout at this path and retains only explicit JSON/report artifacts.
WORKDIR /workspace

USER 10001:10001

ENTRYPOINT ["mutmut"]
