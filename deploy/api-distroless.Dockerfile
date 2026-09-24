# syntax=docker/dockerfile:1.7
# Candidate API runtime with no package manager or shell. Build and scan this
# separately; a successful local build does not update a deployed release.
ARG PYTHON_BUILD_IMAGE=python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.7.20@sha256:2fd1b38e3398a256d6af3f71f0e2ba6a517b249998726a64d8cfbe55ab34af5e
ARG PYTHON_RUNTIME_IMAGE=gcr.io/distroless/python3-debian13:nonroot@sha256:8ee214843129f43e2ebf5e0ca9f2e4e6d8292143d1b8a6787f169b5898578884

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_BUILD_IMAGE} AS build
ARG GCC_RUNTIME_VERSION=14.2.0-19

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       "libgomp1=${GCC_RUNTIME_VERSION}" "libstdc++6=${GCC_RUNTIME_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/bin/python /usr/bin/python

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./

# Invoke venv through the path used by the matching distroless runtime. The
# explicit final links also keep Compose migration/API commands stable.
RUN /usr/bin/python -m venv /opt/venv \
    && uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY config ./config
COPY docs/runbooks ./docs/runbooks
COPY fixtures ./fixtures
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

RUN uv sync --frozen --no-dev \
    && uv cache clean \
    && /opt/venv/bin/python -m pip uninstall --yes pip \
    && rm -f /bin/uv /bin/uvx \
    && rm -f /opt/venv/bin/python /opt/venv/bin/python3 /opt/venv/bin/python3.13 \
    && ln -s /usr/bin/python /opt/venv/bin/python \
    && ln -s python /opt/venv/bin/python3 \
    && ln -s python /opt/venv/bin/python3.13 \
    && /opt/venv/bin/python -c "import lightgbm, prahari.api" \
    && install -d /runtime-libs \
    && set -- /usr/lib/*-linux-gnu/libgomp.so.1 \
    && test "$#" -eq 1 \
    && cp -L "$1" /runtime-libs/ \
    && set -- /usr/lib/*-linux-gnu/libstdc++.so.6 \
    && test "$#" -eq 1 \
    && cp -L "$1" /runtime-libs/ \
    && set -- /usr/lib/*-linux-gnu/libgcc_s.so.1 \
    && test "$#" -eq 1 \
    && cp -L "$1" /runtime-libs/

FROM ${PYTHON_RUNTIME_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LD_LIBRARY_PATH=/opt/prahari/lib \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY --from=build --chown=10001:10001 /opt/venv /opt/venv
COPY --from=build /runtime-libs/ /opt/prahari/lib/
COPY --from=build --chown=10001:10001 /app/src /app/src
COPY --from=build --chown=10001:10001 /app/config /app/config
COPY --from=build --chown=10001:10001 /app/docs /app/docs
COPY --from=build --chown=10001:10001 /app/fixtures /app/fixtures
COPY --from=build --chown=10001:10001 /app/alembic /app/alembic
COPY --from=build --chown=10001:10001 /app/alembic.ini /app/alembic.ini
COPY --from=build --chown=10001:10001 /app/pyproject.toml /app/pyproject.toml

USER 10001:10001
EXPOSE 8080
ENTRYPOINT []
CMD ["/opt/venv/bin/uvicorn", "prahari.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8080"]
