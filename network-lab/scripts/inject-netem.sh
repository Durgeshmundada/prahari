#!/usr/bin/env bash
# Guarded, intentionally limited netem fault injector for the unverified lab.
# It can affect traffic only after explicit acknowledgement. Default mode is a
# dry run and no arbitrary node/interface is accepted.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"

lab_name=""
node=""
interface=""
fault_id=""
loss=""
delay=""
jitter=""
apply=0
confirmation=""

usage() {
  cat <<'USAGE'
Usage:
  bash network-lab/scripts/inject-netem.sh \
    --lab-name prahari --node branch-edge --interface eth1 \
    --fault-id gradual-loss-001 --loss-percent 1.0 --delay-ms 20 --jitter-ms 5

Dry-run is the default. To change the controlled lab interface, also pass:
  --apply --confirm-fault-id gradual-loss-001

Safety limits: loss 0..20%, delay 0..250 ms, jitter 0..100 ms. The only
accepted target is prahari / branch-edge / eth1, the preferred-path egress.
USAGE
}

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_value() {
  [[ -n "${2:-}" ]] || fail "missing value for $1"
}

validate_number() {
  local value="$1"
  local maximum="$2"
  [[ "$value" =~ ^([0-9]+)(\.[0-9]{1,3})?$ ]] || fail "invalid numeric value: $value"
  awk -v value="$value" -v maximum="$maximum" 'BEGIN { exit !(value >= 0 && value <= maximum) }' \
    || fail "value $value is outside 0..$maximum"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lab-name|--node|--interface|--fault-id|--loss-percent|--delay-ms|--jitter-ms|--confirm-fault-id)
      need_value "$1" "${2:-}"
      case "$1" in
        --lab-name) lab_name="$2" ;;
        --node) node="$2" ;;
        --interface) interface="$2" ;;
        --fault-id) fault_id="$2" ;;
        --loss-percent) loss="$2" ;;
        --delay-ms) delay="$2" ;;
        --jitter-ms) jitter="$2" ;;
        --confirm-fault-id) confirmation="$2" ;;
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

[[ "$lab_name" == "prahari" ]] || fail "only the known lab name 'prahari' is accepted"
[[ "$node" == "branch-edge" ]] || fail "only the known node 'branch-edge' is accepted"
[[ "$interface" == "eth1" ]] || fail "only preferred-path egress 'eth1' is accepted"
[[ "$fault_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{2,79}$ ]] || fail "fault ID must be 3-80 safe characters"
validate_number "$loss" 20
validate_number "$delay" 250
validate_number "$jitter" 100

container="clab-${lab_name}-${node}"
if (( apply == 0 )); then
  printf 'DRY RUN: would apply netem to %s:%s (loss=%s%% delay=%sms jitter=%sms).\n' \
    "$container" "$interface" "$loss" "$delay" "$jitter"
  printf 'No lab state was changed. Add --apply --confirm-fault-id %s to proceed.\n' "$fault_id"
  exit 0
fi

[[ "$confirmation" == "$fault_id" ]] || fail "confirmation must exactly match --fault-id"
command -v docker >/dev/null 2>&1 || fail "Docker CLI is required"
docker info >/dev/null || fail "Docker daemon is not reachable by this user"

actual_lab="$(docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null)" \
  || fail "expected lab node not found: $container"
[[ "$actual_lab" == "$lab_name" ]] || fail "refusing unexpected container label on $container"
[[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]] \
  || fail "lab node is not running: $container"
docker exec "$container" sh -c 'command -v tc >/dev/null' \
  || fail "tc is not available in $container"

existing_qdisc="$(docker exec "$container" tc qdisc show dev "$interface")"
printf 'Existing qdisc on %s:%s: %s\n' "$container" "$interface" "$existing_qdisc"
[[ "$existing_qdisc" != *netem* ]] || fail "a netem qdisc already exists; clear it deliberately first"

# Do not replace an operator-installed shaper. The expected fresh veth state is
# noqueue; a plain pfifo/fq_codel fallback is allowed for common host defaults.
[[ "$existing_qdisc" == *noqueue* || "$existing_qdisc" == *pfifo* || "$existing_qdisc" == *fq_codel* ]] \
  || fail "unexpected existing qdisc; refusing to overwrite it"

manifest_dir="$lab_dir/state/faults"
mkdir -p -- "$manifest_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
manifest="$manifest_dir/${timestamp}-${fault_id}.json"
umask 077

cat > "$manifest" <<EOF
{
  "schema_version": "fault-manifest-v1",
  "fault_id": "${fault_id}",
  "status": "planned",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "lab_name": "${lab_name}",
  "node": "${node}",
  "interface": "${interface}",
  "kind": "netem",
  "loss_percent": ${loss},
  "delay_ms": ${delay},
  "jitter_ms": ${jitter}
}
EOF

docker exec "$container" tc qdisc replace dev "$interface" root netem \
  loss "${loss}%" delay "${delay}ms" "${jitter}ms"
sed -i 's/"status": "planned"/"status": "applied"/' "$manifest"

printf 'Fault applied. Manifest: %s\n' "$manifest"
docker exec "$container" tc qdisc show dev "$interface"
