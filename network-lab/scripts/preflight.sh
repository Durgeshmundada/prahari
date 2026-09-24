#!/usr/bin/env bash
# Read-only host and artifact preflight. It does not pull images, deploy a lab,
# load kernel modules, create namespaces, or change host networking.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need_command() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

[[ "$(uname -s)" == "Linux" ]] || fail "a native Linux host or Linux VM is required"
need_command docker
need_command containerlab
need_command ip
need_command grep

docker info >/dev/null || fail "Docker daemon is not reachable by this user"
containerlab version >/dev/null
export PRAHARI_LAB_DATA_DIR="${PRAHARI_LAB_DATA_DIR:-$HOME/.local/share/prahari/noc-api}"
[[ -d "$PRAHARI_LAB_DATA_DIR" && -w "$PRAHARI_LAB_DATA_DIR" ]] \
  || fail "Linux-native lab data directory is missing or not writable: $PRAHARI_LAB_DATA_DIR"

if [[ -r /proc/modules ]] && ! grep -q '^sch_netem ' /proc/modules; then
  fail "kernel module sch_netem is not loaded; ask the host administrator to enable it"
fi

for image in \
  quay.io/frrouting/frr@sha256:e47e67bd612030cb1bedee2bde85b73913f2ea021573b749deb21d94940f03c1 \
  prahari-lab-app:0.1.0; do
  docker image inspect "$image" >/dev/null 2>&1 || fail "required image is not preloaded: $image"
done

bash "$script_dir/validate-topology.sh" "$lab_dir/topology.clab.yml"
printf 'Preflight passed. No lab was deployed and no host state was changed.\n'
