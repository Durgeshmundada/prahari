ARG POSTGRES_IMAGE=postgres@sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73
FROM ${POSTGRES_IMAGE}

# Keep the upstream PostgreSQL 17 entrypoint and data-layout behavior while
# applying the fixed Alpine packages available for this immutable base release.
RUN apk add --no-cache --upgrade \
      libcrypto3=3.5.8-r0 \
      libssl3=3.5.8-r0 \
      libuuid=2.42.3-r1 \
    && apk cache clean \
    && rm -f /usr/local/bin/gosu

# The pinned Alpine base defines postgres as UID/GID 70. Running the upstream
# entrypoint as that account removes its root-to-postgres helper and startup
# privilege transition while retaining the standard entrypoint contract.
USER 70:70
