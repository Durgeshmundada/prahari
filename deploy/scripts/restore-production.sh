#!/usr/bin/env bash
# Restore a verified backup into a new database; never overwrites the active DB.
set -euo pipefail
umask 077

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"
env_file="$deploy_dir/.env.production"
backup_dir=""
target_database=""
confirmation=""
target_created=0
restore_complete=0

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat <<'USAGE'
Usage: sudo bash deploy/scripts/restore-production.sh \
  --env-file ABSOLUTE_OR_RELATIVE_PATH --backup-dir BACKUP_DIRECTORY \
  --target-database NEW_DATABASE --confirm RESTORE_PRODUCTION_BACKUP

The target database must not exist and cannot be the active source database.
The script verifies the release, archive, schema revision, and row counts. It
does not switch the running API to the restored database.
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

cleanup_failed_restore() {
  if (( target_created == 1 && restore_complete == 0 )); then
    printf 'Restore failed; removing newly created target database %s.\n' \
      "$target_database" >&2
    "${compose[@]}" exec -T postgres \
      dropdb --username "$bootstrap_user" --force "$target_database" >/dev/null 2>&1 \
      || printf 'WARNING: manual cleanup may be required for database %s.\n' \
        "$target_database" >&2
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) env_file="${2:-}"; shift 2 ;;
    --backup-dir) backup_dir="${2:-}"; shift 2 ;;
    --target-database) target_database="${2:-}"; shift 2 ;;
    --confirm) confirmation="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ "$(id -u)" -eq 0 ]] || fail "run this production restore with sudo"
[[ "$confirmation" == "RESTORE_PRODUCTION_BACKUP" ]] \
  || fail "explicit --confirm RESTORE_PRODUCTION_BACKUP is required"
[[ -f "$env_file" && ! -L "$env_file" ]] \
  || fail "--env-file must be an existing non-symlink file"
[[ -d "$backup_dir" && ! -L "$backup_dir" ]] \
  || fail "--backup-dir must be an existing non-symlink directory"
[[ "$target_database" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] \
  || fail "--target-database must be a safe lowercase PostgreSQL identifier"
env_file="$(realpath -e -- "$env_file")"
backup_real="$(realpath -e -- "$backup_dir")"
backup_without_links="$(realpath -se -- "$backup_dir")"
[[ "$backup_real" == "$backup_without_links" ]] \
  || fail "backup directory path must not traverse symbolic links"
backup_dir="$backup_real"

bash "$script_dir/validate-compose.sh" "$env_file"
python3 "$script_dir/production-backup-manifest.py" verify \
  --backup-dir "$backup_dir" --env-file "$env_file"
source_database="$(
  python3 "$script_dir/production-backup-manifest.py" inspect \
    --backup-dir "$backup_dir" --env-file "$env_file" --field source_database
)"
[[ "$target_database" != "$source_database" ]] \
  || fail "restore target must differ from the backed-up production database"

bootstrap_user="$(read_env_value POSTGRES_BOOTSTRAP_USER)"
migrator_user="$(read_env_value PRAHARI_MIGRATOR_USER)"
compose=(
  docker compose
  --project-directory "$deploy_dir"
  --env-file "$env_file"
  -f "$deploy_dir/compose.yaml"
  -f "$deploy_dir/compose.production.yaml"
)
"${compose[@]}" ps --status running --services | grep -qx postgres \
  || fail "the validated production PostgreSQL service is not running"
existing="$(
  "${compose[@]}" exec -T postgres psql \
    --username "$bootstrap_user" --dbname postgres \
    --no-align --tuples-only --set ON_ERROR_STOP=1 \
    --command "SELECT 1 FROM pg_database WHERE datname = '$target_database';"
)"
[[ -z "$existing" ]] || fail "target database already exists: $target_database"

trap cleanup_failed_restore EXIT HUP INT TERM
"${compose[@]}" exec -T postgres \
  createdb --username "$bootstrap_user" --owner "$migrator_user" "$target_database"
target_created=1
"${compose[@]}" exec -T postgres \
  pg_restore \
    --username "$bootstrap_user" \
    --dbname "$target_database" \
    --role "$migrator_user" \
    --exit-on-error \
    --single-transaction \
    --no-owner \
    --no-acl < "$backup_dir/database.dump"

# Reuse the release's least-privilege grant job against the new database.
"${compose[@]}" run --rm --no-deps \
  --env "PRAHARI_DATABASE_NAME=$target_database" grant-app-role

restored_revision="$(
  "${compose[@]}" exec -T postgres psql \
    --username "$bootstrap_user" --dbname "$target_database" \
    --no-align --tuples-only --set ON_ERROR_STOP=1 \
    --command 'SELECT version_num FROM alembic_version;'
)"
restored_counts="$(
  "${compose[@]}" exec -T postgres psql \
    --username "$bootstrap_user" --dbname "$target_database" \
    --no-align --tuples-only --set ON_ERROR_STOP=1 \
    --command "SELECT json_build_object(
      'alembic_version', (SELECT count(*) FROM alembic_version),
      'telemetry_records', (SELECT count(*) FROM telemetry_records),
      'app_probe_records', (SELECT count(*) FROM app_probe_records),
      'event_records', (SELECT count(*) FROM event_records),
      'incident_states', (SELECT count(*) FROM incident_states),
      'closure_cases', (SELECT count(*) FROM closure_cases),
      'action_operations', (SELECT count(*) FROM action_operations),
      'browser_sessions', (SELECT count(*) FROM browser_sessions),
      'login_windows', (SELECT count(*) FROM login_windows),
      'audit_events', (SELECT count(*) FROM audit_events)
    )::text;"
)"
python3 "$script_dir/production-backup-manifest.py" verify-restored \
  --backup-dir "$backup_dir" \
  --env-file "$env_file" \
  --alembic-revision "$restored_revision" \
  --counts-json "$restored_counts"
# Never revive sessions that were logged out after the backup was captured.
"${compose[@]}" exec -T postgres psql \
  --username "$bootstrap_user" --dbname "$target_database" \
  --set ON_ERROR_STOP=1 --command 'DELETE FROM browser_sessions;'
restore_complete=1
trap - EXIT HUP INT TERM
printf 'Verified restore completed in new database %s; active database remains %s.\n' \
  "$target_database" "$source_database"
