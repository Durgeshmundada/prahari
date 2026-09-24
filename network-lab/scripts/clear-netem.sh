#!/usr/bin/env bash
# Guarded recovery action for the only netem target supported by this scaffold.
set -euo pipefail

lab_name=""
node=""
interface=""
apply=0
confirmation=""

usage() {
  cat <<'USAGE'
Usage:
  bash network-lab/scripts/clear-netem.sh \
    --lab-name prahari --node branch-edge --interface eth1

Dry-run is the default. To remove an existing netem qdisc, also pass:
  --apply --confirm-clear-netem CLEAR-NETEM
USAGE
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lab-name|--node|--interface|--confirm-clear-netem)
      [[ -n "${2:-}" ]] || fail "missing value for $1"
      case "$1" in
        --lab-name) lab_name="$2" ;;
        --node) node="$2" ;;
        --interface) interface="$2" ;;
        --confirm-clear-netem) confirmation="$2" ;;
      esac
      shift 2
      ;;
    --apply)
      apply=1
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

[[ "$lab_name" == "prahari" || "$lab_name" == "prahari-owned" ]] \
  || fail "only the known controlled lab namespaces are accepted"
[[ "$node" == "branch-edge" ]] || fail "only the known node 'branch-edge' is accepted"
[[ "$interface" == "eth1" ]] || fail "only preferred-path egress 'eth1' is accepted"

container="clab-${lab_name}-${node}"
if (( apply == 0 )); then
  printf 'DRY RUN: would remove a netem qdisc from %s:%s.\n' "$container" "$interface"
  printf 'No lab state was changed. Add --apply --confirm-clear-netem CLEAR-NETEM to proceed.\n'
  exit 0
fi

[[ "$confirmation" == "CLEAR-NETEM" ]] || fail "confirmation must be CLEAR-NETEM"
command -v docker >/dev/null 2>&1 || fail "Docker CLI is required"
docker info >/dev/null || fail "Docker daemon is not reachable by this user"

actual_lab="$(docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null)" \
  || fail "expected lab node not found: $container"
[[ "$actual_lab" == "$lab_name" ]] || fail "refusing unexpected container label on $container"
[[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]] \
  || fail "lab node is not running: $container"

existing_qdisc="$(docker exec "$container" tc qdisc show dev "$interface")"
[[ "$existing_qdisc" == *netem* ]] || fail "no netem qdisc is present; refusing to alter another qdisc"

docker exec "$container" tc qdisc del dev "$interface" root
printf 'Netem qdisc removed from %s:%s.\n' "$container" "$interface"
docker exec "$container" tc qdisc show dev "$interface"
