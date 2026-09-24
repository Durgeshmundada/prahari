# syntax=docker/dockerfile:1.7
# Multi-architecture index digests are reviewed release inputs. Builds remain
# reproducible for a selected platform and cannot silently consume a retagged
# base image.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:9d2e5553305c7c7b0097999bb17187c69b921ccd6bc9d40e4bb5ebe652c00285
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.7.20@sha256:2fd1b38e3398a256d6af3f71f0e2ba6a517b249998726a64d8cfbe55ab34af5e

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=uv /uv /uvx /bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl iproute2 iputils-ping libgomp1 procps \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 prahari \
    && useradd --system --gid prahari --uid 10001 --create-home prahari

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY --chown=prahari:prahari src ./src
COPY --chown=prahari:prahari config ./config
COPY --chown=prahari:prahari docs/runbooks ./docs/runbooks
COPY --chown=prahari:prahari fixtures ./fixtures
COPY --chown=prahari:prahari scripts ./scripts
COPY --chown=prahari:prahari alembic ./alembic
COPY --chown=prahari:prahari alembic.ini ./alembic.ini
RUN install -d -o prahari -g prahari /app/artifacts \
    && uv sync --frozen --no-dev

USER 10001:10001
CMD ["sleep", "infinity"]
