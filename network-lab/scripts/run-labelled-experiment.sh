#!/usr/bin/env bash
# Run a controlled baseline/fault/recovery experiment and retain its labels.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"
samples=12; loss=5; delay=20; jitter=5; confirmation=""
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --samples) samples="${2:-}"; shift 2 ;;
    --loss-percent) loss="${2:-}"; shift 2 ;;
    --delay-ms) delay="${2:-}"; shift 2 ;;
    --jitter-ms) jitter="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    --help|-h) printf 'Usage: %s [--samples 12] [--loss-percent 5] [--delay-ms 20] [--jitter-ms 5] --confirm RUN-LABELLED-EXPERIMENT\n' "$0"; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ "$confirmation" == "RUN-LABELLED-EXPERIMENT" ]] || fail "explicit confirmation is required"
[[ "$samples" =~ ^[0-9]+$ && "$samples" -ge 3 && "$samples" -le 100 ]] || fail "samples must be 3..100"
command -v docker >/dev/null || fail "Docker CLI is required"
command -v jq >/dev/null || fail "jq is required"
sender="clab-prahari-branch-client"
[[ "$(docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$sender" 2>/dev/null)" == "prahari" ]] || fail "PRAHARI branch client is unavailable"
run_id="experiment-$(date -u +%Y%m%dT%H%M%SZ)-${RANDOM}"
fault_id="${run_id}-netem"
result_dir="$lab_dir/state/experiments/$run_id"
mkdir -p -- "$result_dir" "$lab_dir/state/collector"
docker exec --user root "$sender" sh -c 'install -d -o 10001 -g 10001 /var/lib/prahari/path-spool && printf %s 00000000000000000000000000000000 > /run/prahari-lab-api-key && chown 10001:10001 /run/prahari-lab-api-key && chmod 600 /run/prahari-lab-api-key'
fault_active=0
cleanup() {
  if (( fault_active == 1 )); then
    bash "$script_dir/clear-netem.sh" --lab-name prahari --node branch-edge --interface eth1 --apply --confirm-clear-netem CLEAR-NETEM >/dev/null || true
  fi
}
trap cleanup EXIT INT TERM
collect_phase() {
  local phase="$1" output="$result_dir/${1}.log"
  : > "$output"
  for _sample in $(seq 1 "$samples"); do
    docker exec -e PYTHONPATH=/app/src "$sender" /opt/venv/bin/python scripts/lab_path_collector.py --run-id "$run_id" --phase "$phase" --count 5 --api-url http://172.31.255.18:8080 --api-key-file /run/prahari-lab-api-key | tee -a "$output"
  done
}
assert_healthy_phase() {
  local phase="$1" output="$result_dir/${1}.log"
  awk -F'loss=' '
    /loss=/ { split($2, value, " "); total += value[1]; count += 1 }
    END { exit !(count > 0 && total / count <= 0.02) }
  ' "$output" || fail "$phase loss exceeded the 2% healthy-phase guard"
}
collect_phase baseline
assert_healthy_phase baseline
bash "$script_dir/inject-netem.sh" --lab-name prahari --node branch-edge --interface eth1 --fault-id "$fault_id" --loss-percent "$loss" --delay-ms "$delay" --jitter-ms "$jitter" --apply --confirm-fault-id "$fault_id"
fault_active=1
collect_phase impaired
bash "$script_dir/clear-netem.sh" --lab-name prahari --node branch-edge --interface eth1 --apply --confirm-clear-netem CLEAR-NETEM
fault_active=0
collect_phase recovered
assert_healthy_phase recovered
jq -n --arg run_id "$run_id" --arg completed_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --argjson samples "$samples" --argjson loss "$loss" --argjson delay "$delay" --argjson jitter "$jitter" '{schema_version:"labelled-experiment-v1",run_id:$run_id,status:"completed",completed_at:$completed_at,samples_per_phase:$samples,labels:{fault_type:"netem",target:"branch-edge:eth1",loss_percent:$loss,delay_ms:$delay,jitter_ms:$jitter},phases:["baseline","impaired","recovered"]}' > "$result_dir/manifest.json"
printf 'Labelled experiment completed: %s\n' "$result_dir/manifest.json"
