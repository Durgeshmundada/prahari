# syntax=docker/dockerfile:1.7
# Build context: repository root. The release pipeline must use a root
# .dockerignore allowlist before invoking this recipe so developer secrets and
# generated artifacts cannot enter the build context.
# Multi-architecture index digests are deliberate: release builds select their
# target platform while remaining immutable. Refresh them only through the
# reviewed dependency-update process.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.7.20@sha256:2fd1b38e3398a256d6af3f71f0e2ba6a517b249998726a64d8cfbe55ab34af5e

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS runtime
ARG GCC_RUNTIME_VERSION=14.2.0-19
ARG GZIP_VERSION=1.13-1+deb13u1
ARG PCRE2_VERSION=10.46-1~deb13u2
ARG SQLITE_VERSION=3.46.1-7+deb13u2
ARG PERL_BASE_VERSION=5.40.1-6+deb13u1

# LightGBM wheels need the native OpenMP/C++ runtimes. Keep the package version
# aligned with the pinned Debian base and verify imports after every image build.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       "libgomp1=${GCC_RUNTIME_VERSION}" "libstdc++6=${GCC_RUNTIME_VERSION}" \
       "gzip=${GZIP_VERSION}" "libpcre2-8-0=${PCRE2_VERSION}" \
       "libsqlite3-0=${SQLITE_VERSION}" "perl-base=${PERL_BASE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=uv /uv /uvx /bin/

RUN groupadd --system --gid 10001 prahari \
    && useradd --system --gid prahari --uid 10001 --create-home prahari

WORKDIR /app

# Resolve locked runtime dependencies before copying application code, so this
# expensive layer is cached when only source code changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=prahari:prahari src ./src
COPY --chown=prahari:prahari config ./config
COPY --chown=prahari:prahari docs/runbooks ./docs/runbooks
COPY --chown=prahari:prahari fixtures ./fixtures
COPY --chown=prahari:prahari alembic ./alembic
COPY --chown=prahari:prahari alembic.ini ./alembic.ini

RUN uv sync --frozen --no-dev \
    && uv cache clean \
    && /usr/local/bin/python -m pip uninstall --yes pip \
    && rm -f /bin/uv /bin/uvx

USER 10001:10001
EXPOSE 8080

CMD ["/opt/venv/bin/uvicorn", "prahari.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8080"]
