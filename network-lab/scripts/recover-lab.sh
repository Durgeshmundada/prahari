#!/usr/bin/env bash
# Idempotently restore the exact PRAHARI topology and workload after Docker restarts.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"
confirmation=""
[[ "${1:-}" == "--confirm" ]] && confirmation="${2:-}"
[[ "$confirmation" == "RECOVER-PRAHARI-LAB" ]] || {
  printf 'ERROR: --confirm RECOVER-PRAHARI-LAB is required\n' >&2
  exit 1
}
command -v docker >/dev/null
command -v containerlab >/dev/null
command -v flock >/dev/null
docker info >/dev/null

# Containerlab validates bind sources before creating nodes. The app image runs
# as uid/gid 10001, so prepare only its dedicated state directories.
install -d -m 0770 "$lab_dir/state/collector"
if [[ "$(id -u)" -eq 0 ]]; then
  chown 10001:10001 "$lab_dir/state/collector"
fi
export PRAHARI_LAB_DATA_DIR="${PRAHARI_LAB_DATA_DIR:-$HOME/.local/share/prahari/noc-api}"
# Rootless recovery cannot chown to the container uid. This directory contains
# lab-only SQLite state on the private WSL filesystem; production uses Postgres.
install -d -m 0777 "$PRAHARI_LAB_DATA_DIR"

secret_dir="$lab_dir/state/secrets"
token_file="$secret_dir/traffic-token"
action_token_file="$secret_dir/action-agent-token"
install -d -m 700 "$secret_dir"
for secret_file in "$token_file" "$action_token_file"; do
  if [[ ! -s "$secret_file" ]]; then
    umask 077
    python3 -c 'import secrets; print(secrets.token_hex(32))' > "$secret_file"
  fi
  secret_value="$(tr -d '\r\n' < "$secret_file")"
  [[ "$secret_value" =~ ^[a-f0-9]{64}$ ]] \
    || { printf 'ERROR: invalid stored secret: %s\n' "$secret_file" >&2; exit 1; }
done

exec 9>"/tmp/prahari-lab-recovery.lock"
flock -n 9 || { printf 'Another PRAHARI recovery is already running.\n'; exit 0; }

needs_deploy=0
for node in branch-client branch-edge pe-primary core pe-secondary pe-backup dc-edge app-server noc-api; do
  container="clab-prahari-${node}"
  if [[ "$(docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null || true)" != "prahari" \
    || "$(docker inspect --format '{{.State.Running}}' "$container" 2>/dev/null || true)" != "true" ]]; then
    needs_deploy=1
  fi
done

# A running container is not enough: recovery must converge to the immutable
# image reference declared for that node. Otherwise a source/image update can
# leave an apparently healthy but stale lab running indefinitely.
expected_image_for_node() {
  local node="$1"
  awk -v marker="    ${node}:" '
    $0 == marker { in_node = 1; next }
    in_node && /^      image:/ { print $2; exit }
    in_node && /^    [^ ]/ { exit }
  ' "$lab_dir/topology.clab.yml"
}
for node in branch-client branch-edge pe-primary core pe-secondary pe-backup dc-edge app-server noc-api; do
  expected_image="$(expected_image_for_node "$node")"
  [[ -n "$expected_image" ]] || {
    printf 'ERROR: topology has no immutable image for node %s\n' "$node" >&2
    exit 1
  }
  actual_image="$(docker inspect --format '{{.Config.Image}}' "clab-prahari-${node}" 2>/dev/null || true)"
  [[ "$actual_image" == "$expected_image" ]] || needs_deploy=1
done
for endpoint in branch-client app-server branch-edge dc-edge; do
  docker exec "clab-prahari-${endpoint}" ip link show eth1 >/dev/null 2>&1 || needs_deploy=1
done

if (( needs_deploy == 1 )); then
  containerlab deploy --topo "$lab_dir/topology.clab.yml" --reconfigure
  # A recreated topology needs the full convergence and data-plane check.
  bash "$script_dir/runtime-check.sh" --lab-name prahari
else
  # The periodic keeper must stay cheap enough not to disturb the five-second
  # telemetry cadence. Route/neighbor checks are sufficient while every
  # expected container and interface is already healthy; the collectors
  # continuously exercise the data plane themselves.
  bash "$script_dir/runtime-check.sh" --lab-name prahari --skip-traffic >/dev/null
fi

ollama_image="${PRAHARI_OLLAMA_IMAGE:-ollama/ollama@sha256:020e4134285e2ef4d8fd801234176de3b4faadc992a3eb06c8e66a2f9d4c4ba2}"
ollama_model="${PRAHARI_OLLAMA_MODEL:-qwen3:0.6b}"
ollama_ready=0
if [[ "$(docker inspect --format '{{.State.Running}}' prahari-ollama 2>/dev/null || true)" == "true" \
  && "$(docker inspect --format '{{.Config.Image}}' prahari-ollama 2>/dev/null || true)" == "$ollama_image" \
  && "$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' prahari-ollama 2>/dev/null || true)" == "172.31.255.19" \
  && "$(docker exec prahari-ollama ollama list 2>/dev/null | awk 'NR > 1 {print $1}' | grep -Fx "$ollama_model" || true)" == "$ollama_model" ]]; then
  ollama_ready=1
fi
if (( ollama_ready == 0 )); then
  bash "$script_dir/start-local-copilot.sh"
fi

token="$(tr -d '\r\n' < "$token_file")"

sender_running="$(docker exec clab-prahari-branch-client pgrep -f traffic_demo_probe.py 2>/dev/null || true)"
receiver_running="$(docker exec clab-prahari-app-server pgrep -f traffic_demo_receiver.py 2>/dev/null || true)"
if [[ -z "$sender_running" || -z "$receiver_running" ]]; then
  bash "$script_dir/stop-app-traffic.sh" >/dev/null
  PRAHARI_LAB_TRAFFIC_TOKEN="$token" bash "$script_dir/start-app-traffic.sh"
fi
printf 'PRAHARI lab recovery completed and the workload is running.\n'
