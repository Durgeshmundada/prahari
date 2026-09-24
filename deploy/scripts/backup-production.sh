#!/usr/bin/env bash
# Create an atomic, checksum-bound PostgreSQL backup for the validated release.
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"
env_file="$deploy_dir/.env.production"
output=""
confirmation=""
temporary=""

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Usage: sudo bash deploy/scripts/backup-production.sh \
  --env-file ABSOLUTE_OR_RELATIVE_PATH --output ABSOLUTE_OR_RELATIVE_DIRECTORY \
  --confirm BACKUP_PRODUCTION

The output directory must not exist. No secret values are copied into it.
USAGE
}

read_env_value() {
  local key="$1"
  local value
  value="$(awk -v requested_key="$key" '
    index($0, requested_key "=") == 1 {
      print substr($0, length(requested_key) + 2)
      exit
    }
  ' "$env_file")"
  [[ -n "$value" ]] || fail "missing $key in $env_file"
  printf '%s' "$value"
}

cleanup_partial() {
  [[ -n "$temporary" && -d "$temporary" ]] || return 0
  rm -f -- \
    "$temporary/database.dump" \
    "$temporary/archive.list" \
    "$temporary/manifest.json" \
    "$temporary/manifest.sha256"
  rmdir -- "$temporary" 2>/dev/null || true
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) env_file="${2:-}"; shift 2 ;;
    --output) output="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || fail "run this production backup with sudo"
[[ "$confirmation" == "BACKUP_PRODUCTION" ]] \
  || fail "explicit --confirm BACKUP_PRODUCTION is required"
[[ -n "$output" ]] || fail "--output is required"
[[ -f "$env_file" && ! -L "$env_file" ]] \
  || fail "--env-file must be an existing non-symlink file"
env_file="$(realpath -e -- "$env_file")"

output_parent="$(dirname -- "$output")"
output_name="$(basename -- "$output")"
[[ "$output_name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$ ]] \
  || fail "backup directory name must be a safe 3-100 character identifier"
[[ -d "$output_parent" && ! -L "$output_parent" ]] \
  || fail "backup parent must be an existing non-symlink directory"
parent_real="$(realpath -e -- "$output_parent")"
parent_without_links="$(realpath -se -- "$output_parent")"
[[ "$parent_real" == "$parent_without_links" ]] \
  || fail "backup parent path must not traverse symbolic links"
output="$parent_real/$output_name"
[[ ! -e "$output" && ! -L "$output" ]] || fail "backup output already exists: $output"
temporary="$parent_real/.${output_name}.partial.$$"
[[ ! -e "$temporary" && ! -L "$temporary" ]] \
  || fail "backup staging path already exists: $temporary"
trap cleanup_partial EXIT HUP INT TERM

bash "$script_dir/validate-compose.sh" "$env_file"
database_name="$(read_env_value PRAHARI_DATABASE_NAME)"
bootstrap_user="$(read_env_value POSTGRES_BOOTSTRAP_USER)"
compose=(
  docker compose
  --project-directory "$deploy_dir"
  --env-file "$env_file"
  -f "$deploy_dir/compose.yaml"
  -f "$deploy_dir/compose.production.yaml"
)
"${compose[@]}" ps --status running --services | grep -qx postgres \
  || fail "the validated production PostgreSQL service is not running"

mkdir -m 700 -- "$temporary"
snapshot_metadata="$(
  python3 "$script_dir/consistent-postgres-backup.py" \
    --user "$bootstrap_user" --database "$database_name" \
    --output "$temporary/database.dump" -- "${compose[@]}" exec -T postgres
)"
[[ -s "$temporary/database.dump" ]] || fail "pg_dump produced an empty archive"
"${compose[@]}" exec -T postgres pg_restore --list \
  < "$temporary/database.dump" > "$temporary/archive.list"
grep -q 'TABLE DATA' "$temporary/archive.list" \
  || fail "PostgreSQL archive contains no table data"
alembic_revision="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["alembic_revision"])' <<< "$snapshot_metadata")"
table_counts="$(python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["table_counts"]))' <<< "$snapshot_metadata")"
created_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
python3 "$script_dir/production-backup-manifest.py" create \
  --backup-dir "$temporary" \
  --env-file "$env_file" \
  --database-name "$database_name" \
  --alembic-revision "$alembic_revision" \
  --counts-json "$table_counts" \
  --created-at "$created_at"
rm -f -- "$temporary/archive.list"
chmod 600 \
  "$temporary/database.dump" \
  "$temporary/manifest.json" \
  "$temporary/manifest.sha256"
mv -- "$temporary" "$output"
temporary=""
trap - EXIT HUP INT TERM
printf 'Production backup completed and verified: %s\n' "$output"
