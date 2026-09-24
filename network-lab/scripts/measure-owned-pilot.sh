#!/usr/bin/env bash
# Collect actual traffic on the owned pilot; no fixture/timestamp substitution.
set -euo pipefail
state_dir="$(realpath -e -- "${1:?Usage: measure-owned-pilot.sh STATE [samples]}")"
samples="${2:-12}"
exec 9>"$state_dir/measurement.lock"
flock -x 9
[[ "$samples" =~ ^[0-9]+$ && "$samples" -ge 1 && "$samples" -le 10000 ]] || exit 1
jq -e '.lab == "prahari-owned" and .action_adapter_enabled == false' \
  "$state_dir/pilot-manifest.json" >/dev/null
sender=clab-prahari-owned-branch-client
[[ "$(docker inspect -f '{{ index .Config.Labels "containerlab" }}' "$sender")" == prahari-owned ]]
router=clab-prahari-owned-branch-edge
[[ "$(docker inspect -f '{{ index .Config.Labels "containerlab" }}' "$router")" == prahari-owned ]]
docker exec "$router" tc -j qdisc show dev eth1 | jq -e 'all(.[]; .kind != "netem")' >/dev/null \
  || { printf 'Normal collection refused: owned path still has an active fault.\n' >&2; exit 1; }
mkdir -p -- "$state_dir/measurements"
output="$state_dir/measurements/normal-$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}.jsonl"
for ((sequence=0;sequence<samples;sequence++)); do
  started="$SECONDS"
  docker exec "$sender" sh -c \
    'export PRAHARI_TRAFFIC_DEMO_TOKEN="$(cat /run/prahari-traffic-token)"; exec /opt/venv/bin/python scripts/predictive_probe.py "$@"' \
    probe --lab-name prahari-owned --run-id continuous-path --sequence "$sequence" --scenario normal --phase baseline \
    --loss-percent 0 --delay-ms 0 --jitter-ms 0 --api-url http://172.30.254.18:8080 \
    >> "$output"
  remaining=$((5 - (SECONDS - started)))
  if (( remaining > 0 )); then sleep "$remaining"; fi
done
printf 'Actual joined measurements saved: %s\n' "$output"
