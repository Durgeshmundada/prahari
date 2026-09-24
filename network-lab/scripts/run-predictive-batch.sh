#!/usr/bin/env bash
# Run the minimum balanced set of independent predictive-v2 lab experiments.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"
runs_per_class=12
base_seed=20260903
confirmation=""

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --runs-per-class) runs_per_class="${2:-}"; shift 2 ;;
    --base-seed) base_seed="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    --help|-h)
      printf 'Usage: %s [--runs-per-class 12] [--base-seed 20260903] --confirm RUN-PREDICTIVE-BATCH\n' "$0"
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done
[[ "$runs_per_class" =~ ^[0-9]+$ && "$runs_per_class" -ge 12 && "$runs_per_class" -le 60 ]] \
  || fail "--runs-per-class must be 12..60"
[[ "$base_seed" =~ ^[0-9]+$ ]] || fail "--base-seed must be a non-negative integer"
# Retries consume new deterministic seeds. Reserve the complete bounded retry
# range now instead of failing close to the signed 32-bit schema ceiling.
maximum_seed_span=$((runs_per_class * 12 + 1))
(( base_seed <= 2147483647 - maximum_seed_span )) \
  || fail "--base-seed leaves insufficient room for the bounded retry schedule"
[[ "$confirmation" == "RUN-PREDICTIVE-BATCH" ]] || fail "explicit confirmation is required"

for command in jq sed tail; do
  command -v "$command" >/dev/null || fail "$command is required"
done

profiles=(a b c)
traffic_profiles=(light standard heavy)
batch_id="predictive-batch-$(date -u +%Y%m%dT%H%M%SZ)-s${base_seed}-${RANDOM}"
batch_log_dir="$lab_dir/state/predictive-batch-logs/$batch_id"
[[ ! -e "$batch_log_dir" ]] || fail "batch log directory already exists"
mkdir -p -- "$batch_log_dir"

normal_count=0
normal_attempt=0
impacted_runs=0
fault_attempt=0
nonimpact_fault_runs=0

write_summary() {
  local status="$1"
  local temporary="$batch_log_dir/summary.json.tmp"
  jq -n \
    --arg schema_version predictive-batch-summary-v2 \
    --arg batch_id "$batch_id" \
    --arg status "$status" \
    --arg recorded_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson requested_runs_per_class "$runs_per_class" \
    --argjson normal_runs "$normal_count" \
    --argjson normal_attempts "$normal_attempt" \
    --argjson normal_failed_attempts "$((normal_attempt - normal_count))" \
    --argjson impacted_runs "$impacted_runs" \
    --argjson nonimpact_fault_runs "$nonimpact_fault_runs" \
    --argjson fault_attempts "$fault_attempt" \
    --argjson fault_failed_attempts \
      "$((fault_attempt - impacted_runs - nonimpact_fault_runs))" \
    '{schema_version:$schema_version,batch_id:$batch_id,status:$status,
      recorded_at:$recorded_at,requested_runs_per_class:$requested_runs_per_class,
      normal_runs:$normal_runs,normal_attempts:$normal_attempts,
      normal_failed_attempts:$normal_failed_attempts,impacted_runs:$impacted_runs,
      nonimpact_fault_runs:$nonimpact_fault_runs,fault_attempts:$fault_attempts,
      fault_failed_attempts:$fault_failed_attempts}' > "$temporary"
  mv -- "$temporary" "$batch_log_dir/summary.json"
}

# Even an interrupted or quota-exhausted campaign leaves a durable summary next
# to its per-attempt logs. Unsealed child evidence remains in predictive-experiments
# and is deliberately rejected by the exporter.
trap 'batch_exit_status=$?; if (( batch_exit_status != 0 )); then write_summary incomplete; fi' EXIT

declare -A normal_target normal_success fault_target fault_success impact_traffic_success
base_target=$((runs_per_class / 3))
target_remainder=$((runs_per_class % 3))
for index in "${!traffic_profiles[@]}"; do
  traffic="${traffic_profiles[$index]}"
  target="$base_target"
  if (( index < target_remainder )); then target=$((target + 1)); fi
  normal_target[$traffic]="$target"
  normal_success[$traffic]=0
  impact_traffic_success[$traffic]=0
done
for index in "${!profiles[@]}"; do
  profile="${profiles[$index]}"
  target="$base_target"
  if (( index < target_remainder )); then target=$((target + 1)); fi
  fault_target[$profile]="$target"
  fault_success[$profile]=0
done

run_and_capture() {
  local log_file="$1"
  shift
  local result status
  if result="$("$@" 2>&1)"; then
    status=0
  else
    status=$?
  fi
  {
    printf 'exit_status=%s\n' "$status"
    printf '%s\n' "$result"
  } > "$log_file"
  printf '%s\n' "$result"
  RUN_RESULT="$result"
  RUN_STATUS="$status"
}

maximum_normal_attempts=$((runs_per_class * 3))
while (( normal_count < runs_per_class && normal_attempt < maximum_normal_attempts )); do
  traffic_profile=""
  for offset in 0 1 2; do
    candidate="${traffic_profiles[$(((normal_attempt + offset) % 3))]}"
    if (( normal_success[$candidate] < normal_target[$candidate] )); then
      traffic_profile="$candidate"
      break
    fi
  done
  [[ -n "$traffic_profile" ]] || fail "normal traffic-profile quota accounting failed"
  normal_seed=$((base_seed + normal_attempt * 2))
  log_file="$batch_log_dir/normal-attempt-$(printf '%03d' "$normal_attempt")-s${normal_seed}.log"
  run_and_capture "$log_file" bash "$script_dir/run-predictive-experiment.sh" \
    --scenario normal --normal-samples "$((42 + normal_seed % 13))" \
    --experiment-seed "$normal_seed" --traffic-profile "$traffic_profile" \
    --confirm RUN-PREDICTIVE-V2
  if (( RUN_STATUS != 0 )); then
    printf 'Normal attempt %s failed; evidence/log retained and retrying.\n' "$normal_attempt" >&2
    normal_attempt=$((normal_attempt + 1))
    continue
  fi
  manifest_path="$(sed -n 's/^Predictive experiment sealed: //p' <<<"$RUN_RESULT" | tail -n 1)"
  if [[ ! -f "$manifest_path" || -L "$manifest_path" ]] \
    || ! jq -e --argjson seed "$normal_seed" --arg traffic "$traffic_profile" \
      '.status == "completed" and .scenario == "normal" and
       .actual_impact_start_time == null and .experiment_seed == $seed and
       .traffic_profile_id == $traffic' "$manifest_path" >/dev/null; then
    printf 'Normal attempt %s did not yield the expected sealed manifest; retrying.\n' \
      "$normal_attempt" >&2
    normal_attempt=$((normal_attempt + 1))
    continue
  fi
  normal_success[$traffic_profile]=$((normal_success[$traffic_profile] + 1))
  normal_count=$((normal_count + 1))
  normal_attempt=$((normal_attempt + 1))
done
(( normal_count == runs_per_class )) \
  || fail "only $normal_count valid normal runs were sealed after $normal_attempt bounded attempts"

minimum_impact_traffic_runs=3
impact_targets_met() {
  local item
  (( impacted_runs >= runs_per_class )) || return 1
  for item in "${profiles[@]}"; do
    (( fault_success[$item] >= fault_target[$item] )) || return 1
  done
  for item in "${traffic_profiles[@]}"; do
    (( impact_traffic_success[$item] >= minimum_impact_traffic_runs )) || return 1
  done
  return 0
}

maximum_fault_attempts=$((runs_per_class * 6))
while ! impact_targets_met && (( fault_attempt < maximum_fault_attempts )); do
  profile=""
  for offset in 0 1 2; do
    candidate="${profiles[$(((fault_attempt + offset) % 3))]}"
    if (( fault_success[$candidate] < fault_target[$candidate] )); then
      profile="$candidate"
      break
    fi
  done
  # Once fault-family quotas are full, additional attempts may still be needed
  # to obtain three independently impacted traffic profiles.
  if [[ -z "$profile" ]]; then profile="${profiles[$((fault_attempt % 3))]}"; fi
  # Cycle the traffic axis independently of the fault axis. Across nine
  # attempts this covers the complete 3x3 profile matrix instead of confounding
  # one application load with one fault family.
  traffic_profile="${traffic_profiles[$(((fault_attempt / 3) % 3))]}"
  fault_seed=$((base_seed + fault_attempt * 2 + 1))
  log_file="$batch_log_dir/fault-attempt-$(printf '%03d' "$fault_attempt")-s${fault_seed}.log"
  run_and_capture "$log_file" bash "$script_dir/run-predictive-experiment.sh" \
    --scenario gradual_loss --profile "$profile" --experiment-seed "$fault_seed" \
    --traffic-profile "$traffic_profile" --confirm RUN-PREDICTIVE-V2
  if (( RUN_STATUS != 0 )); then
    printf 'Fault attempt %s failed; evidence/log retained and retrying.\n' "$fault_attempt" >&2
    fault_attempt=$((fault_attempt + 1))
    continue
  fi
  manifest_path="$(sed -n 's/^Predictive experiment sealed: //p' <<<"$RUN_RESULT" | tail -n 1)"
  if [[ ! -f "$manifest_path" || -L "$manifest_path" ]] \
    || ! jq -e --argjson seed "$fault_seed" --arg profile "$profile" \
      --arg traffic "$traffic_profile" \
      '.status == "completed" and .scenario == "gradual_loss" and
       .experiment_seed == $seed and .fault_profile_id == $profile and
       .traffic_profile_id == $traffic' "$manifest_path" >/dev/null; then
    printf 'Fault attempt %s did not yield the expected sealed manifest; retrying.\n' \
      "$fault_attempt" >&2
    fault_attempt=$((fault_attempt + 1))
    continue
  fi
  if jq -e '.actual_impact_start_time != null' "$manifest_path" >/dev/null; then
    fault_success[$profile]=$((fault_success[$profile] + 1))
    impact_traffic_success[$traffic_profile]=$((impact_traffic_success[$traffic_profile] + 1))
    impacted_runs=$((impacted_runs + 1))
  else
    nonimpact_fault_runs=$((nonimpact_fault_runs + 1))
    printf 'Fault run had no sustained application impact; retaining it as no-impact evidence.\n'
  fi
  fault_attempt=$((fault_attempt + 1))
done
impact_targets_met \
  || fail "impact/profile quotas were not met after $fault_attempt bounded fault attempts"

write_summary completed
trap - EXIT
printf 'Completed %s valid normal and %s impacted runs (%s no-impact fault runs retained).\n' \
  "$normal_count" "$impacted_runs" "$nonimpact_fault_runs"
printf 'Batch evidence: %s\n' "$batch_log_dir"
