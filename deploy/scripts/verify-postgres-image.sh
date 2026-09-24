#!/usr/bin/env bash
# Prove a hardened PostgreSQL candidate can initialize and serve data without root or gosu.
set -euo pipefail

image_ref="${1:?candidate image reference required}"
[[ "$EUID" == 0 && "$image_ref" =~ ^[a-zA-Z0-9./:@_-]+$ ]]
image_id="$(docker image inspect "$image_ref" | jq -er '.[0].Id')"
[[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]]
[[ "$(docker image inspect "$image_id" | jq -er '.[0].Config.User')" == "70:70" ]]

suffix="$(date +%s)-${RANDOM}"
container="prahari-postgres-verify-$suffix"
volume="prahari-postgres-verify-$suffix"
password="$(cat /proc/sys/kernel/random/uuid)"
cleanup() {
  if [[ "$(docker inspect -f '{{ index .Config.Labels "prahari.postgres.verify" }}' "$container" 2>/dev/null)" == true ]]; then
    docker rm -f "$container" >/dev/null || true
  fi
  if [[ "$(docker volume inspect -f '{{ index .Labels "prahari.postgres.verify" }}' "$volume" 2>/dev/null)" == true ]]; then
    docker volume rm "$volume" >/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker volume create --label prahari.postgres.verify=true "$volume" >/dev/null
docker run -d --name "$container" --label prahari.postgres.verify=true \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 70:70 --memory 768m --cpus 1 --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,uid=70,gid=70 \
  --tmpfs /var/run/postgresql:rw,noexec,nosuid,size=16m,uid=70,gid=70 \
  --mount "type=volume,src=$volume,dst=/var/lib/postgresql/data" \
  --env POSTGRES_PASSWORD="$password" "$image_id" >/dev/null

for _ in $(seq 1 90); do
  if docker exec "$container" pg_isready -U postgres -d postgres >/dev/null 2>&1; then
    break
  fi
  if [[ "$(docker inspect -f '{{.State.Running}}' "$container")" != true ]]; then
    docker logs "$container" >&2
    exit 1
  fi
  sleep 1
done
docker exec "$container" pg_isready -U postgres -d postgres >/dev/null
docker exec "$container" sh -eu -c 'test ! -e /usr/local/bin/gosu && test "$(id -u)" = 70'
docker exec "$container" psql -v ON_ERROR_STOP=1 -U postgres -d postgres \
  -c 'CREATE TABLE candidate_probe (id integer PRIMARY KEY); INSERT INTO candidate_probe VALUES (1);' \
  >/dev/null
rows="$(docker exec "$container" psql -At -U postgres -d postgres -c 'SELECT count(*) FROM candidate_probe;')"
[[ "$rows" == 1 ]]
printf 'PostgreSQL candidate initialized and served data as UID 70: %s\n' "$image_id"
