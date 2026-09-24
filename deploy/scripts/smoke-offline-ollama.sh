#!/usr/bin/env bash
# Prove the pinned Ollama image can load and infer from a local read-only store.
# The temporary container has no network interface and is always removed.
set -euo pipefail

image=""
model=""
models_dir=""
models_volume=""

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Usage: smoke-offline-ollama.sh --image NAME@sha256:DIGEST --model NAME:TAG \
  (--models-dir ABSOLUTE_PATH | --models-volume DOCKER_VOLUME)
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --image) image="${2:-}"; shift 2 ;;
    --model) model="${2:-}"; shift 2 ;;
    --models-dir) models_dir="${2:-}"; shift 2 ;;
    --models-volume) models_volume="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] \
  || fail "--image must be an immutable name@sha256 digest"
[[ "$model" =~ ^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$ ]] \
  || fail "--model must contain a safe explicit tag"
[[ -n "$models_dir" && -z "$models_volume" \
  || -z "$models_dir" && -n "$models_volume" ]] \
  || fail "choose exactly one local model source"

mount_args=()
if [[ -n "$models_dir" ]]; then
  [[ "$models_dir" = /* && -d "$models_dir" && ! -L "$models_dir" ]] \
    || fail "--models-dir must be an existing absolute non-symlink directory"
  mount_args=(--mount "type=bind,src=$models_dir,dst=/opt/ollama/models,readonly")
else
  [[ "$models_volume" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] \
    || fail "--models-volume is invalid"
  docker volume inspect "$models_volume" >/dev/null \
    || fail "Docker model volume does not exist: $models_volume"
  # The standard Ollama volume stores model content below its `models/`
  # directory. Mount that subdirectory so this exercises the same layout as
  # PRAHARI_OLLAMA_MODELS_DIR in the production overlay.
  mount_args=(--mount "type=volume,src=$models_volume,dst=/opt/ollama/models,readonly,volume-subpath=models")
fi

docker image inspect "$image" >/dev/null \
  || fail "the exact Ollama image is not preloaded"
container="prahari-ollama-smoke-$$"
[[ "$container" =~ ^prahari-ollama-smoke-[0-9]+$ ]] || fail "unsafe temporary name"
docker inspect "$container" >/dev/null 2>&1 \
  && fail "temporary container already exists: $container"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker run -d \
  --name "$container" \
  --network none \
  --user 10001:10001 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,mode=1777,size=1g \
  --tmpfs /home/ollama:rw,nosuid,nodev,mode=0700,uid=10001,gid=10001,size=16m \
  "${mount_args[@]}" \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --env HOME=/home/ollama \
  --env OLLAMA_HOST=127.0.0.1:11434 \
  --env OLLAMA_MODELS=/opt/ollama/models \
  --env OLLAMA_NOHISTORY=true \
  --env OLLAMA_NO_CLOUD=true \
  --env OLLAMA_NOPRUNE=true \
  "$image" serve >/dev/null

ready=0
for _attempt in $(seq 1 60); do
  if docker exec "$container" ollama list >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if (( ready == 0 )); then
  docker logs "$container" >&2 || true
  fail "Ollama did not become ready"
fi

[[ "$(docker inspect --format '{{.Config.User}}' "$container")" == "10001:10001" ]] \
  || fail "smoke container is not running as the required non-root identity"
[[ "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$container")" == "none" ]] \
  || fail "smoke container unexpectedly has a network"
[[ "$(docker inspect --format '{{.HostConfig.ReadonlyRootfs}}' "$container")" == "true" ]] \
  || fail "smoke container root filesystem is writable"
model_mount_rw="$(docker inspect --format '{{range .Mounts}}{{println .Destination .RW}}{{end}}' "$container" \
  | awk '$1 == "/opt/ollama/models" { print $2 }')"
[[ "$model_mount_rw" == "false" ]] \
  || fail "smoke container model store is writable"
[[ "$(docker exec "$container" id -u)" == "10001" ]] \
  || fail "Ollama process identity is root"
docker exec "$container" /bin/sh -c \
  'test ! -w /opt/ollama/models && ! touch /opt/ollama/models/.prahari-write-test 2>/dev/null' \
  || fail "model store accepted a write"
docker exec "$container" /bin/sh -c \
  '! touch /.prahari-write-test 2>/dev/null' \
  || fail "root filesystem accepted a write"
docker exec "$container" ollama show "$model" >/dev/null \
  || fail "the requested local model is unavailable or corrupt"
inference="$(timeout 180 docker exec "$container" ollama run "$model" 'Reply with READY.')" \
  || fail "local offline inference failed"
[[ -n "${inference//[[:space:]]/}" ]] || fail "local offline inference returned no content"

printf 'Offline Ollama smoke passed: non-root, read-only, networkless, model=%s.\n' "$model"
