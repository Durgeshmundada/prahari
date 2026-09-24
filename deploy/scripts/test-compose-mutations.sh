#!/usr/bin/env bash
# Render the production template and prove known security regressions fail closed.
# This only runs `docker compose config`; it never starts or changes Docker objects.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"
env_file="${1:-$deploy_dir/.env.production.example}"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

[[ -f "$env_file" && ! -L "$env_file" ]] \
  || fail "production environment template must be a regular non-symlink file"
command -v docker >/dev/null 2>&1 || fail "Docker CLI is required"
command -v python3 >/dev/null 2>&1 || fail "Python 3 is required"
docker compose version >/dev/null

rendered_model="$(mktemp)"
trap 'rm -f "$rendered_model"' EXIT HUP INT TERM
docker compose \
  --project-directory "$deploy_dir" \
  --env-file "$env_file" \
  -f "$deploy_dir/compose.yaml" \
  -f "$deploy_dir/compose.production.yaml" \
  config --format json > "$rendered_model"
python3 "$script_dir/test-compose-mutations.py" "$rendered_model"
