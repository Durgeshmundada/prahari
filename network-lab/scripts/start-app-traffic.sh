#!/usr/bin/env bash
# Starts the bounded receiver and repeating branch probe on the named lab nodes.
set -euo pipefail

lab_name="prahari"
receiver="clab-${lab_name}-app-server"
sender="clab-${lab_name}-branch-client"
api="clab-${lab_name}-noc-api"
token="${PRAHARI_LAB_TRAFFIC_TOKEN:-}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ "$token" =~ ^[A-Fa-f0-9]{32,128}$ ]] \
  || fail "PRAHARI_LAB_TRAFFIC_TOKEN must contain 32-128 hexadecimal characters"

for container in "$receiver" "$sender" "$api"; do
  actual_lab="$(
    docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null
  )" || fail "expected lab node not found: $container"
  [[ "$actual_lab" == "$lab_name" ]] || fail "refusing unexpected container: $container"
  [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]] \
    || fail "lab node is not running: $container"
done

docker exec "$api" curl --fail --silent --show-error --max-time 3 http://127.0.0.1:8080/health \
  >/dev/null || fail "PRAHARI management API is not ready"

if docker exec "$receiver" pgrep -f 'traffic_demo_receiver.py' >/dev/null; then
  fail "traffic receiver is already running"
fi
docker exec -d \
  -e PRAHARI_TRAFFIC_DEMO_TOKEN="$token" \
  "$receiver" \
  sh -c '/opt/venv/bin/python scripts/traffic_demo_receiver.py --host 172.16.20.2 --port 8765 --allow-non-loopback > /tmp/prahari-receiver.log 2>&1'

receiver_ready=0
for _attempt in $(seq 1 20); do
  if docker exec "$receiver" curl --fail --silent --max-time 2 http://172.16.20.2:8765/health >/dev/null; then
    receiver_ready=1
    break
  fi
  sleep 0.5
done
[[ "$receiver_ready" -eq 1 ]] || fail "traffic receiver did not become ready"

if docker exec "$sender" pgrep -f 'traffic_demo_probe.py' >/dev/null; then
  fail "traffic probe is already running"
fi
docker exec -d \
  -e PRAHARI_TRAFFIC_DEMO_TOKEN="$token" \
  "$sender" \
  sh -c '/opt/venv/bin/python scripts/traffic_demo_probe.py --receiver-url ws://172.16.20.2:8765/ws/traffic --collector-url http://172.31.255.18:8080 --allow-non-loopback-receiver --allow-insecure-remote-ws --repeat 0 > /tmp/prahari-probe.log 2>&1'

printf 'Application traffic started: %s -> %s; evidence API: %s.\n' \
  "$sender" "$receiver" "$api"
