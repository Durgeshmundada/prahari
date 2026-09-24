#!/usr/bin/env bash
# Generate a bounded diverse seed set; each experiment remains independently split-able.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
confirmation="${1:-}"
[[ "$confirmation" == "GENERATE-PRAHARI-SEED-DATASET" ]] || {
  printf 'ERROR: pass GENERATE-PRAHARI-SEED-DATASET to confirm six controlled faults\n' >&2
  exit 1
}
specifications=("2 10 2" "4 15 3" "6 25 5" "8 35 8" "10 45 10" "3 20 4")
for specification in "${specifications[@]}"; do
  read -r loss delay jitter <<<"$specification"
  bash "$script_dir/run-labelled-experiment.sh" --samples 3 \
    --loss-percent "$loss" --delay-ms "$delay" --jitter-ms "$jitter" \
    --confirm RUN-LABELLED-EXPERIMENT
done
printf 'Six-run PRAHARI seed dataset generation completed.\n'
