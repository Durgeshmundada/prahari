#!/usr/bin/env bash
# Resume one verified owned-server cohort. Completed cohorts are a no-op on reboot.
set -euo pipefail

state_dir="$(realpath -e -- "${1:?owned pilot state directory required}")"
summary="$(realpath -e -- "${2:?campaign summary required}")"
collector="$(realpath -e -- "${3:?frozen resume collector required}")"
runs_per_class="${4:?runs per class required}"
base_seed="${5:?base seed required}"
delay_scale="${6:?delay scale required}"
normal_samples="${7:-600}"

case "$summary" in
  "$state_dir"/model-campaigns/*/summary.json) ;;
  *) printf 'ERROR: campaign summary is outside the owned pilot\n' >&2; exit 2 ;;
esac
case "$collector" in
  "$state_dir"/campaign-resumers/*/scripts/run_owned_model_campaign.py) ;;
  *) printf 'ERROR: resume collector is outside the protected release area\n' >&2; exit 2 ;;
esac
[[ "$runs_per_class" =~ ^[0-9]+$ && "$base_seed" =~ ^[0-9]+$ \
  && "$delay_scale" =~ ^[0-9]+$ && "$normal_samples" =~ ^[0-9]+$ ]] || {
  printf 'ERROR: campaign parameters must be unsigned integers\n' >&2
  exit 2
}

status="$(python3 - "$summary" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
    raise SystemExit("invalid campaign summary")
value = json.loads(path.read_bytes())
if not isinstance(value, dict) or not isinstance(value.get("status"), str):
    raise SystemExit("invalid campaign status")
print(value["status"])
PY
)"

if [[ "$status" == "collection_complete" ]]; then
  printf 'Owned model cohort is already complete; no resume required.\n'
  exit 0
fi
if [[ "$status" != "running" && "$status" != "incomplete" ]]; then
  printf 'ERROR: campaign status %s is not resumable\n' "$status" >&2
  exit 2
fi

ready_deadline=$((SECONDS + 1800))
until docker exec clab-prahari-owned-branch-edge ip link show eth1 >/dev/null 2>&1; do
  if (( SECONDS >= ready_deadline )); then
    printf 'ERROR: owned lab did not expose controlled eth1 within 30 minutes\n' >&2
    exit 75
  fi
  sleep 15
done

exec python3 "$collector" \
  --state-dir "$state_dir" \
  --runs-per-class "$runs_per_class" \
  --base-seed "$base_seed" \
  --delay-scale-percent "$delay_scale" \
  --normal-samples "$normal_samples" \
  --resume-summary "$summary"
