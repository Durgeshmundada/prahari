#!/usr/bin/env bash
# Foreground WSL client keeps the VM alive and supervises our pilot only.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
state_dir="$(realpath -e -- "${1:?prepared state directory required}")"
exec 8>"$state_dir/supervisor.lock"
flock -n 8 || { printf 'Owned pilot supervisor already running.\n'; exit 0; }
while true; do
  if ! docker exec clab-prahari-owned-branch-edge ip link show eth1 >/dev/null 2>&1; then
    bash "$script_dir/start-owned-pilot.sh" "$state_dir" || { sleep 15; continue; }
  fi
  if ! bash "$script_dir/measure-owned-pilot.sh" "$state_dir" 12; then
    printf 'Measurement failed; no model evidence approved. Rechecking owned services.\n' >&2
    bash "$script_dir/start-owned-pilot.sh" "$state_dir" || true
    sleep 10
  fi
done
