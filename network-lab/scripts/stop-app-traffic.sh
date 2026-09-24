#!/usr/bin/env bash
# Stops only the known PRAHARI traffic processes inside the two lab endpoints.
set -euo pipefail

for target in \
  'clab-prahari-branch-client:traffic_demo_probe.py' \
  'clab-prahari-app-server:traffic_demo_receiver.py'; do
  container="${target%%:*}"
  process_pattern="${target#*:}"
  actual_lab="$(
    docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null
  )" || continue
  [[ "$actual_lab" == "prahari" ]] || continue
  docker exec "$container" pkill -TERM -f "$process_pattern" 2>/dev/null || true
done

printf 'Known PRAHARI application traffic processes were stopped.\n'
