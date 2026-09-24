#!/usr/bin/env bash
# Start the local-only Ollama runtime and ensure PRAHARI's selected model exists.
set -euo pipefail

image="${PRAHARI_OLLAMA_IMAGE:-ollama/ollama@sha256:020e4134285e2ef4d8fd801234176de3b4faadc992a3eb06c8e66a2f9d4c4ba2}"
model="${PRAHARI_OLLAMA_MODEL:-qwen3:0.6b}"
container="prahari-ollama"
network="prahari_mgmt"
address="172.31.255.19"
volume="prahari_ollama_models"

docker network inspect "$network" >/dev/null
docker volume create "$volume" >/dev/null
if ! docker image inspect "$image" >/dev/null 2>&1; then
  docker pull "$image"
fi

existing_image="$(docker inspect --format '{{.Config.Image}}' "$container" 2>/dev/null || true)"
existing_network="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$container" 2>/dev/null || true)"
if [[ "$existing_image" != "$image" || "$existing_network" != "$address" ]]; then
  docker rm -f "$container" >/dev/null 2>&1 || true
fi
if ! docker inspect "$container" >/dev/null 2>&1; then
  docker run -d \
    --name "$container" \
    --restart unless-stopped \
    --network "$network" \
    --ip "$address" \
    --volume "$volume:/root/.ollama" \
    --security-opt no-new-privileges:true \
    "$image" >/dev/null
else
  docker start "$container" >/dev/null
fi

for _ in $(seq 1 60); do
  if docker exec "$container" ollama list >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$container" ollama list >/dev/null
if ! docker exec "$container" ollama list | awk 'NR > 1 {print $1}' | grep -Fxq "$model"; then
  docker exec "$container" ollama pull "$model"
fi
docker exec "$container" ollama show "$model" >/dev/null
printf 'Local copilot runtime ready: %s at %s (%s)\n' "$model" "$address" "$container"
