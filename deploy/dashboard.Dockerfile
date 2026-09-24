# syntax=docker/dockerfile:1.7
# Build context: repository root. The root .dockerignore prevents credentials,
# artefacts, and node_modules from entering the build context.
ARG NODE_IMAGE=node:22.14-alpine@sha256:9bef0ef1e268f60627da9ba7d7605e8831d5b56ad07487d24d1aa386336d1944
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:stable-alpine@sha256:adf5042a17f4ecdd200c595fa9ffd1be37efb18f89a830bd1a00e4ab4d59d42c

FROM ${NODE_IMAGE} AS build
WORKDIR /app
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --ignore-scripts
COPY apps/dashboard ./
ARG VITE_API_BASE_URL=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM ${NGINX_IMAGE} AS runtime
COPY deploy/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
