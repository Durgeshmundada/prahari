#!/usr/bin/env bash
# Read-only verification for an already deployed lab. The optional ping sends
# ordinary probe traffic but does not alter routing, container, or host state.
set -euo pipefail

lab_name=""
skip_traffic=0

usage() {
  cat <<'USAGE'
Usage: bash network-lab/scripts/runtime-check.sh --lab-name prahari [--skip-traffic]

Checks only the known PRAHARI lab and refuses arbitrary container targets.
USAGE
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lab-name)
      lab_name="${2:-}"
      shift 2
      ;;
    --skip-traffic)
      skip_traffic=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[[ "$lab_name" == "prahari" ]] || fail "only the known lab name 'prahari' is accepted"
command -v docker >/dev/null 2>&1 || fail "Docker CLI is required"
docker info >/dev/null || fail "Docker daemon is not reachable by this user"

masquerade="$({
  docker network inspect prahari_mgmt \
    --format '{{ index .Options "com.docker.network.bridge.enable_ip_masquerade" }}'
} 2>/dev/null || true)"
[[ "$masquerade" == "false" ]] \
  || fail "prahari_mgmt is not using the required no-masquerade zero-egress policy"

require_running_node() {
  local node="$1"
  local container="clab-${lab_name}-${node}"
  local actual_lab

  actual_lab="$(docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null)" \
    || fail "expected lab node not found: $container"
  [[ "$actual_lab" == "$lab_name" ]] || fail "refusing unexpected container label on $container"
  [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]] \
    || fail "lab node is not running: $container"
}

for node in \
  branch-client branch-edge pe-primary core pe-secondary pe-backup dc-edge app-server noc-api; do
  require_running_node "$node"
done

# A fresh OSPF broadcast network needs time for neighbor election and database
# exchange. Poll real readiness instead of relying on a machine-dependent sleep.
ready=0
for _attempt in $(seq 1 45); do
  forward_candidate="$(
    docker exec "clab-${lab_name}-branch-edge" \
      vtysh -c 'show ip route 172.16.20.0/24' 2>/dev/null || true
  )"
  return_candidate="$(
    docker exec "clab-${lab_name}-dc-edge" \
      vtysh -c 'show ip route 172.16.10.0/24' 2>/dev/null || true
  )"
  neighbor_candidate="$(
    docker exec "clab-${lab_name}-branch-edge" \
      vtysh -c 'show ip ospf neighbor' 2>/dev/null || true
  )"
  candidate_full="$(printf '%s\n' "$neighbor_candidate" | grep -c ' Full/' || true)"
  if [[ "$forward_candidate" == *'via eth1'* \
    && "$return_candidate" == *'via eth1'* \
    && "$candidate_full" -eq 2 ]]; then
    ready=1
    break
  fi
  sleep 2
done
[[ "$ready" -eq 1 ]] || fail "OSPF did not converge on both primary routes within 90 seconds"

printf '%s\n' '--- Preferred and backup routes from branch-edge ---'
forward_route="$(
  docker exec "clab-${lab_name}-branch-edge" \
    vtysh -c 'show ip route 172.16.20.0/24'
)"
printf '%s\n' "$forward_route"
[[ "$forward_route" == *'Known via "ospf"'* && "$forward_route" == *'via eth1'* ]] \
  || fail "branch-to-application route is not using the primary path"

return_route="$(
  docker exec "clab-${lab_name}-dc-edge" \
    vtysh -c 'show ip route 172.16.10.0/24'
)"
[[ "$return_route" == *'Known via "ospf"'* && "$return_route" == *'via eth1'* ]] \
  || fail "application-to-branch route is not using the primary path"

printf '%s\n' '--- OSPF neighbor state on branch-edge ---'
neighbors="$(
  docker exec "clab-${lab_name}-branch-edge" vtysh -c 'show ip ospf neighbor'
)"
printf '%s\n' "$neighbors"
full_neighbors="$(printf '%s\n' "$neighbors" | grep -c ' Full/' || true)"
[[ "$full_neighbors" -eq 2 ]] || fail "branch-edge does not have two Full OSPF neighbors"

if (( skip_traffic == 0 )); then
  printf '%s\n' '--- ICMP probe from branch-client to app-server ---'
  probe="$(
    docker exec "clab-${lab_name}-branch-client" ping -n -c 3 -W 2 172.16.20.2
  )"
  printf '%s\n' "$probe"
  [[ "$probe" == *'3 received, 0% packet loss'* ]] \
    || fail "branch-to-application ICMP validation did not complete without loss"
fi

printf 'Runtime check completed without changing the lab configuration.\n'
