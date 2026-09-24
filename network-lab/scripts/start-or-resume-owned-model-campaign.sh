#!/usr/bin/env bash
# Start one parameter-bound campaign or resume its uniquely matching checkpoint.
set -euo pipefail

state_dir="$(realpath -e -- "${1:?owned pilot state directory required}")"
collector="$(realpath -e -- "${2:?protected campaign collector required}")"
runs_per_class="${3:?runs per class required}"
base_seed="${4:?base seed required}"
delay_scale="${5:?delay scale required}"
normal_samples="${6:-600}"

case "$collector" in
  "$state_dir"/campaign-resumers/*/scripts/run_owned_model_campaign.py) ;;
  *) printf 'ERROR: collector is outside the protected release area\n' >&2; exit 2 ;;
esac
[[ "$runs_per_class" =~ ^[0-9]+$ && "$base_seed" =~ ^[0-9]+$ \
  && "$delay_scale" =~ ^[0-9]+$ && "$normal_samples" =~ ^[0-9]+$ ]] || {
  printf 'ERROR: campaign parameters must be unsigned integers\n' >&2
  exit 2
}

selection="$(python3 - "$state_dir" "$collector" "$runs_per_class" "$base_seed" "$delay_scale" "$normal_samples" <<'PY'
import hashlib
import json
import pathlib
import sys

state = pathlib.Path(sys.argv[1])
collector = pathlib.Path(sys.argv[2])
runs_per_class, base_seed, delay_scale, normal_samples = map(int, sys.argv[3:])
collector_sha256 = hashlib.sha256(collector.read_bytes()).hexdigest()
matches = []
campaigns = state / "model-campaigns"
if campaigns.is_dir() and not campaigns.is_symlink():
    for summary in campaigns.glob("*/summary.json"):
        if summary.is_symlink() or not summary.is_file() or summary.stat().st_size > 2 * 1024 * 1024:
            continue
        value = json.loads(summary.read_bytes())
        expected = (
            value.get("schema_version") == "owned-model-campaign-v1"
            and value.get("campaign_source_sha256") == collector_sha256
            and value.get("requested_runs_per_class") == runs_per_class
            and value.get("base_seed") == base_seed
            and value.get("delay_scale_percent") == delay_scale
            and value.get("normal_samples") == normal_samples
            and value.get("model_approved") is False
        )
        if expected:
            matches.append((summary.resolve(strict=True), value.get("status")))
if len(matches) > 1:
    raise SystemExit("multiple campaigns match this immutable collection identity")
if not matches:
    print("new\t-")
else:
    path, status = matches[0]
    if status not in {"running", "incomplete", "collection_complete"}:
        raise SystemExit(f"matching campaign has non-resumable status: {status}")
    print(f"{status}\t{path}")
PY
)"
status="${selection%%$'\t'*}"
summary="${selection#*$'\t'}"

if [[ "$status" == "collection_complete" ]]; then
  printf 'Owned model cohort is already complete: %s\n' "$summary"
  exit 0
fi

ready_deadline=$((SECONDS + 1800))
until docker exec clab-prahari-owned-branch-edge ip link show eth1 >/dev/null 2>&1; do
  if (( SECONDS >= ready_deadline )); then
    printf 'ERROR: owned lab did not expose controlled eth1 within 30 minutes\n' >&2
    exit 75
  fi
  sleep 15
done

arguments=(
  --state-dir "$state_dir"
  --runs-per-class "$runs_per_class"
  --base-seed "$base_seed"
  --delay-scale-percent "$delay_scale"
  --normal-samples "$normal_samples"
)
if [[ "$status" != "new" ]]; then
  arguments+=(--resume-summary "$summary")
fi
exec python3 "$collector" "${arguments[@]}"
