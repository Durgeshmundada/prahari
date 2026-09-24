#!/usr/bin/env bash
# Capture one gradual-impact or long normal run for predictive-v2.
set -euo pipefail
export LC_ALL=C

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"
repo_dir="$(cd -- "$lab_dir/.." && pwd)"
scenario=""
profile="a"
experiment_seed=20260903
traffic_profile="standard"
normal_samples=42
cadence=5
confirmation=""
lab_name="prahari"
state_dir=""
delay_scale_percent=100

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'USAGE'
Usage: bash network-lab/scripts/run-predictive-experiment.sh \
  --scenario normal|gradual_loss [--profile a|b|c] \
  [--experiment-seed 20260903] [--traffic-profile light|standard|heavy] \
  [--normal-samples 42] [--cadence-seconds 5] \
  [--lab-name prahari|prahari-owned] [--state-dir /prepared/state] \
  [--delay-scale-percent 100..250] \
  --confirm RUN-PREDICTIVE-V2

The target is the selected controlled lab's branch-edge:eth1. Every sample joins a
real path probe with the same-path chat/file application probe.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --scenario) scenario="${2:-}"; shift 2 ;;
    --profile) profile="${2:-}"; shift 2 ;;
    --experiment-seed) experiment_seed="${2:-}"; shift 2 ;;
    --traffic-profile) traffic_profile="${2:-}"; shift 2 ;;
    --normal-samples) normal_samples="${2:-}"; shift 2 ;;
    --cadence-seconds) cadence="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    --lab-name) lab_name="${2:-}"; shift 2 ;;
    --state-dir) state_dir="${2:-}"; shift 2 ;;
    --delay-scale-percent) delay_scale_percent="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$scenario" == "normal" || "$scenario" == "gradual_loss" ]] \
  || fail "--scenario must be normal or gradual_loss"
[[ "$profile" =~ ^[abc]$ ]] || fail "--profile must be a, b, or c"
[[ "$experiment_seed" =~ ^[0-9]+$ && "$experiment_seed" -le 2147483647 ]] \
  || fail "--experiment-seed must be 0..2147483647"
[[ "$traffic_profile" == "light" || "$traffic_profile" == "standard" || "$traffic_profile" == "heavy" ]] \
  || fail "--traffic-profile must be light, standard, or heavy"
[[ "$normal_samples" =~ ^[0-9]+$ && "$normal_samples" -ge 42 && "$normal_samples" -le 1200 ]] \
  || fail "--normal-samples must be 42..1200"
[[ "$cadence" == "5" ]] || fail "predictive-v2 freezes --cadence-seconds at 5"
[[ "$confirmation" == "RUN-PREDICTIVE-V2" ]] || fail "explicit confirmation is required"
[[ "$lab_name" == "prahari" || "$lab_name" == "prahari-owned" ]] || fail "unknown controlled lab namespace"
[[ "$delay_scale_percent" =~ ^[0-9]+$ && "$delay_scale_percent" -ge 100 && "$delay_scale_percent" -le 250 ]] \
  || fail "delay scale must be 100..250 percent"
[[ "$delay_scale_percent" == 100 || "$lab_name" == prahari-owned ]] \
  || fail "additional delay scaling is restricted to the owned pilot"
state_dir="${state_dir:-$lab_dir/state}"
state_dir="$(realpath -e -- "$state_dir")"
api_url="http://172.31.255.18:8080"
if [[ "$lab_name" == "prahari-owned" ]]; then
  [[ "$EUID" == 0 ]] || fail "owned pilot experiments require its Linux server administrator"
  jq -e '.lab == "prahari-owned" and .action_adapter_enabled == false' "$state_dir/pilot-manifest.json" >/dev/null
  api_url="http://172.30.254.18:8080"
  exec 9>"$state_dir/measurement.lock"
  flock -x 9
fi

for command in awk docker jq; do
  command -v "$command" >/dev/null || fail "$command is required"
done
sender="clab-${lab_name}-branch-client"
receiver="clab-${lab_name}-app-server"
router="clab-${lab_name}-branch-edge"
containers=("$sender" "$receiver" "$router")
if [[ "$lab_name" == "prahari" ]]; then containers+=(clab-prahari-noc-api); fi
for container in "${containers[@]}"; do
  actual_lab="$(docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$container" 2>/dev/null)" \
    || fail "expected container is unavailable: $container"
  [[ "$actual_lab" == "$lab_name" ]] || fail "refusing unexpected container: $container"
  [[ "$(docker inspect --format '{{.State.Running}}' "$container")" == "true" ]] \
    || fail "container is not running: $container"
done
docker exec "$router" ip link show eth1 >/dev/null || fail "controlled eth1 is unavailable"
docker exec "$sender" curl --fail --silent --max-time 3 "$api_url/health" >/dev/null \
  || fail "PRAHARI API is unavailable"

token_file="$state_dir/secrets/traffic-token"
[[ -f "$token_file" && ! -L "$token_file" ]] || fail "traffic token file is unavailable"
token="$(tr -d '\r\n' < "$token_file")"
[[ "$token" =~ ^[A-Fa-f0-9]{32,128}$ ]] || fail "traffic token is malformed"
token_env_file="$state_dir/secrets/traffic-process.env"
[[ ! -L "$token_env_file" ]] || fail "traffic environment file must not be a symlink"
(umask 077; printf 'PRAHARI_TRAFFIC_DEMO_TOKEN=%s\n' "$token" > "$token_env_file")
chmod 600 "$token_env_file"
unset token
if ! docker exec "$receiver" pgrep -f 'traffic_demo_receiver.py' >/dev/null; then
  docker exec -d --env-file "$token_env_file" "$receiver" \
    sh -c '/opt/venv/bin/python scripts/traffic_demo_receiver.py --host 172.16.20.2 --port 8765 --allow-non-loopback > /tmp/prahari-receiver.log 2>&1'
  ready=0
  for _attempt in $(seq 1 20); do
    if docker exec "$receiver" curl --fail --silent --max-time 2 \
      http://172.16.20.2:8765/health >/dev/null; then
      ready=1
      break
    fi
    sleep 0.5
  done
  [[ "$ready" -eq 1 ]] || fail "application receiver did not become ready"
fi
if [[ "$lab_name" == "prahari" ]]; then
docker exec --user root "$sender" sh -c \
  'install -d -o 10001 -g 10001 /var/lib/prahari/path-spool && printf %s 00000000000000000000000000000000 > /run/prahari-lab-api-key && chown 10001:10001 /run/prahari-lab-api-key && chmod 600 /run/prahari-lab-api-key'
fi

existing_qdisc="$(docker exec "$router" tc qdisc show dev eth1)"
[[ "$existing_qdisc" != *netem* ]] \
  || fail "controlled interface already has netem; clear it deliberately first"

case "$traffic_profile" in
  light) chat_messages=3; file_kib=2 ;;
  standard) chat_messages=5; file_kib=4 ;;
  heavy) chat_messages=9; file_kib=16 ;;
esac

run_id="predictive-${scenario}-$(date -u +%Y%m%dT%H%M%SZ)-s${experiment_seed}-${RANDOM}"
run_dir="$state_dir/predictive-experiments/$run_id"
[[ ! -e "$run_dir" ]] || fail "run directory already exists"
mkdir -p -- "$run_dir"
if [[ "$lab_name" == "prahari-owned" ]]; then chown 10001:10001 "$run_dir"; chmod 700 "$run_dir"; fi
observations="$run_dir/observations.jsonl"
fault_schedule="$run_dir/fault-schedule.jsonl"
: > "$observations"
utc_now() { date -u +%Y-%m-%dT%H:%M:%S.%6NZ; }
started_at="$(utc_now)"
fault_active=0
sequence=0

cleanup() {
  if (( fault_active == 1 )); then
    bash "$script_dir/clear-netem.sh" --lab-name "$lab_name" --node branch-edge \
      --interface eth1 --apply --confirm-clear-netem CLEAR-NETEM >/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

apply_fault_step() {
  local step_sequence="$1" loss="$2" delay="$3" jitter="$4" qdisc_json
  jq -cn --arg target "${router}:eth1" --arg run_id "$run_id" --arg at "$(utc_now)" \
    --argjson sequence "$step_sequence" --argjson loss "$loss" \
    --argjson delay "$delay" --argjson jitter "$jitter" \
    '{schema_version:"predictive-fault-step-v2",run_id:$run_id,state:"planned",recorded_at:$at,target:$target,sequence:$sequence,condition:{loss_percent:$loss,delay_ms:$delay,jitter_ms:$jitter}}' \
    >> "$fault_schedule"
  docker exec "$router" tc qdisc replace dev eth1 root netem \
    loss "${loss}%" delay "${delay}ms" "${jitter}ms"
  fault_active=1
  qdisc_json="$(docker exec "$router" tc -j qdisc show dev eth1)"
  jq -e '([.[] | select(.kind == "netem" and .root == true)] | length) == 1' \
    <<<"$qdisc_json" >/dev/null || fail "netem qdisc verification failed"
  jq -cn --arg target "${router}:eth1" --arg run_id "$run_id" --arg at "$(utc_now)" \
    --argjson sequence "$step_sequence" --argjson loss "$loss" \
    --argjson delay "$delay" --argjson jitter "$jitter" --argjson qdisc "$qdisc_json" \
    '{schema_version:"predictive-fault-step-v2",run_id:$run_id,state:"applied",recorded_at:$at,target:$target,sequence:$sequence,condition:{loss_percent:$loss,delay_ms:$delay,jitter_ms:$jitter},qdisc_snapshot:$qdisc}' \
    >> "$fault_schedule"
}

clear_fault() {
  local step_sequence="$1" qdisc_json
  jq -cn --arg target "${router}:eth1" --arg run_id "$run_id" --arg at "$(utc_now)" --argjson sequence "$step_sequence" \
    '{schema_version:"predictive-fault-step-v2",run_id:$run_id,state:"clear_planned",recorded_at:$at,target:$target,sequence:$sequence,condition:{loss_percent:0,delay_ms:0,jitter_ms:0}}' \
    >> "$fault_schedule"
  bash "$script_dir/clear-netem.sh" --lab-name "$lab_name" --node branch-edge \
    --interface eth1 --apply --confirm-clear-netem CLEAR-NETEM >/dev/null
  qdisc_json="$(docker exec "$router" tc -j qdisc show dev eth1)"
  jq -e 'all(.[]; .kind != "netem")' <<<"$qdisc_json" >/dev/null \
    || fail "netem qdisc remains after controlled clear"
  jq -cn --arg target "${router}:eth1" --arg run_id "$run_id" --arg at "$(utc_now)" --argjson sequence "$step_sequence" \
    --argjson qdisc "$qdisc_json" \
    '{schema_version:"predictive-fault-step-v2",run_id:$run_id,state:"cleared",recorded_at:$at,target:$target,sequence:$sequence,condition:{loss_percent:0,delay_ms:0,jitter_ms:0},qdisc_snapshot:$qdisc}' \
    >> "$fault_schedule"
  fault_active=0
}

collect_sample() {
  local phase="$1" loss="$2" delay="$3" jitter="$4"
  local sample_started elapsed remaining output
  sample_started="$(date +%s)"
  output="$(
    docker exec -e PYTHONPATH=/app/src --env-file "$token_env_file" "$sender" \
      /opt/venv/bin/python scripts/predictive_probe.py \
      --lab-name "$lab_name" --api-url "$api_url" --run-id "$run_id" --sequence "$sequence" --scenario "$scenario" --phase "$phase" \
      --loss-percent "$loss" --delay-ms "$delay" --jitter-ms "$jitter" \
      --chat-messages "$chat_messages" --file-kib "$file_kib"
  )" || fail "joined predictive probe failed at sequence $sequence"
  jq -e -c 'select(.schema_version == "predictive-observation-v2")' <<<"$output" >> "$observations" \
    || fail "predictive probe returned invalid JSON at sequence $sequence"
  sequence=$((sequence + 1))
  elapsed=$(($(date +%s) - sample_started))
  remaining=$((cadence - elapsed))
  if (( remaining > 0 )); then sleep "$remaining"; fi
}

if [[ "$scenario" == "normal" ]]; then
  for _sample in $(seq 1 "$normal_samples"); do
    collect_sample baseline 0 0 0
  done
else
  baseline_samples=$((12 + experiment_seed % 7))
  ramp_dwell_samples=$((1 + (experiment_seed / 7) % 3))
  steady_samples=$((6 + (experiment_seed / 21) % 7))
  recovery_samples=$((12 + (experiment_seed / 147) % 7))
  for _sample in $(seq 1 "$baseline_samples"); do
    collect_sample baseline 0 0 0
  done
  case "$profile" in
    a)
      losses=(0.5 1 1.5 2 3 4 5 6 8 10 12 14)
      delays=(5 10 15 20 25 30 40 50 60 70 80 90)
      jitters=(1 1 2 2 3 4 5 6 8 10 12 15)
      ;;
    b)
      losses=(0.5 0.8 1.2 1.8 2.5 3.5 4.5 6 7.5 9 11 13)
      delays=(0 5 10 15 20 25 35 45 55 65 75 85)
      jitters=(1 2 2 3 4 5 6 8 10 12 14 18)
      ;;
    c)
      losses=(0.2 0.5 1 1.5 2 3 4 5.5 7 9 12 16)
      delays=(10 15 20 25 30 35 40 50 60 75 90 110)
      jitters=(0 1 2 3 4 5 7 9 11 14 18 22)
      ;;
  esac
  # Keep each profile's safe monotonic shape while varying its absolute
  # severity deterministically by seed. This prevents repeated runs from
  # teaching the model one literal scripted sequence.
  severity_scale_percent=$((85 + (experiment_seed % 7) * 5))
  for index in "${!losses[@]}"; do
    losses[$index]="$(awk -v value="${losses[$index]}" -v scale="$severity_scale_percent" \
      'BEGIN { printf "%.3f", value * scale / 100 }')"
    delays[$index]="$(awk -v value="${delays[$index]}" -v scale="$severity_scale_percent" \
      -v delay_scale="$delay_scale_percent" 'BEGIN { delay=value * scale * delay_scale / 10000; printf "%.3f", (delay > 250 ? 250 : delay) }')"
    jitters[$index]="$(awk -v value="${jitters[$index]}" -v scale="$severity_scale_percent" \
      'BEGIN { printf "%.3f", value * scale / 100 }')"
    # Linux netem ignores jitter when base delay is zero. Record the effective
    # condition rather than claiming the ignored requested jitter was applied.
    if awk -v delay="${delays[$index]}" 'BEGIN { exit delay != 0 }'; then
      jitters[$index]=0
    fi
  done
  for index in "${!losses[@]}"; do
    apply_fault_step "$sequence" "${losses[$index]}" "${delays[$index]}" "${jitters[$index]}"
    for _sample in $(seq 1 "$ramp_dwell_samples"); do
      collect_sample ramp "${losses[$index]}" "${delays[$index]}" "${jitters[$index]}"
    done
  done
  for _sample in $(seq 1 "$steady_samples"); do
    collect_sample steady "${losses[-1]}" "${delays[-1]}" "${jitters[-1]}"
  done
  clear_fault "$sequence"
  for _sample in $(seq 1 "$recovery_samples"); do
    collect_sample recovery 0 0 0
  done
fi

finalize_args=(
  --run-dir /evidence --run-id "$run_id" --scenario "$scenario"
  --started-at "$started_at" --experiment-seed "$experiment_seed"
  --traffic-profile "$traffic_profile" --sampling-seconds "$cadence"
  --policy /workspace/config/predictive-v2.json
)
if [[ "$scenario" == "gradual_loss" ]]; then
  finalize_args+=(--fault-profile "$profile" --fault-file /evidence/fault-schedule.jsonl)
else
  finalize_args+=(--fault-profile none)
fi
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user 10001:10001 --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --mount "type=bind,src=$repo_dir,dst=/workspace,readonly" \
  --mount "type=bind,src=$run_dir,dst=/evidence" \
  --env PYTHONPATH=/workspace/src --workdir /workspace \
  prahari-lab-app@sha256:66624bf03c707cbe2ae19a72aedbca3c9d5cfdf424ee6a9cc529d2378d5bfb9e \
  /opt/venv/bin/python scripts/finalize_predictive_run.py "${finalize_args[@]}" \
  > "$run_dir/finalize-output.json"
printf 'Predictive experiment sealed: %s\n' "$run_dir/manifest.json"
