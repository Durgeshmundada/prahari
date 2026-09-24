#!/usr/bin/env bash
# Parse and validate the Containerlab topology. Containerlab's validate command
# performs schema/node/link checks without deploying or destroying a lab.
set -euo pipefail

export PRAHARI_LAB_DATA_DIR="${PRAHARI_LAB_DATA_DIR:-$HOME/.local/share/prahari/noc-api}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"
topology="${1:-$lab_dir/topology.clab.yml}"

if ! command -v containerlab >/dev/null 2>&1; then
  printf 'ERROR: containerlab is required (this check never deploys a lab).\n' >&2
  exit 1
fi

if [[ ! -f "$topology" ]]; then
  printf 'ERROR: topology file not found: %s\n' "$topology" >&2
  exit 1
fi

containerlab validate -t "$topology"
printf 'Containerlab topology validation completed without deployment.\n'
