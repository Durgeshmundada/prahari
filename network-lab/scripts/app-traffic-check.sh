#!/usr/bin/env bash
# Read-only validation of the running application workload and persisted receipt.
set -euo pipefail

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "Docker CLI is required"
command -v jq >/dev/null 2>&1 || fail "jq is required"

sender="clab-prahari-branch-client"
receiver="clab-prahari-app-server"
api="clab-prahari-noc-api"
for container in "$sender" "$receiver" "$api"; do
  actual_lab="$(
    docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null
  )" || fail "expected lab node not found: $container"
  [[ "$actual_lab" == "prahari" ]] || fail "refusing unexpected container: $container"
done

docker exec "$sender" pgrep -f 'traffic_demo_probe.py' >/dev/null \
  || fail "branch traffic probe is not running"
docker exec "$receiver" pgrep -f 'traffic_demo_receiver.py' >/dev/null \
  || fail "application receiver is not running"

evidence="$(
  docker exec "$api" curl --fail --silent http://127.0.0.1:8080/api/demo/traffic
)" || fail "traffic evidence API is unavailable"

status="$(jq -r '.status' <<<"$evidence")"
integrity="$(jq -r '.payload.integrity' <<<"$evidence")"
expected="$(jq -r '.payload.size_bytes' <<<"$evidence")"
received="$(jq -r '.payload.bytes_received' <<<"$evidence")"
[[ "$status" == "passed" ]] || fail "latest application run did not pass"
[[ "$integrity" == "verified" ]] || fail "latest application receipt is not verified"
[[ "$expected" -gt 0 && "$received" -eq "$expected" ]] \
  || fail "latest receiver byte count is incomplete"

printf '%s\n' "$evidence" | jq '{
  status,
  updated_at,
  path_id,
  payload: {
    name: .payload.name,
    size_bytes: .payload.size_bytes,
    bytes_received: .payload.bytes_received,
    integrity: .payload.integrity
  },
  latency_ms,
  throughput_mbps,
  loss_percent
}'
printf 'Application traffic and persisted evidence are healthy.\n'
