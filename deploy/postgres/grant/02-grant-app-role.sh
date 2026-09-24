#!/bin/sh
# Runs after Alembic. It makes audit_events append-only for the API database
# role while allowing the exact telemetry and incident-state operations needed
# by the current persistence layer.
set -eu

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

require_identifier() {
  value="$1"
  label="$2"
  printf '%s' "$value" | grep -Eq '^[a-z_][a-z0-9_]{0,62}$' \
    || fail "$label must be a lowercase PostgreSQL identifier of at most 63 characters"
}

read_password_file() {
  path="$1"
  [ -n "$path" ] || fail "bootstrap password file path is empty"
  [ -r "$path" ] || fail "bootstrap password file is not readable: $path"
  [ "$(wc -l < "$path")" -le 1 ] || fail "bootstrap password must contain one line"
  value="$(tr -d '\r\n' < "$path")"
  [ -n "$value" ] || fail "bootstrap password is empty"
  case "$value" in
    CHANGE_ME*|REPLACE*|EXAMPLE*) fail "bootstrap password still contains a placeholder" ;;
  esac
  printf '%s' "$value"
}

require_identifier "$POSTGRES_BOOTSTRAP_USER" "POSTGRES_BOOTSTRAP_USER"
require_identifier "$PRAHARI_DATABASE_NAME" "PRAHARI_DATABASE_NAME"
require_identifier "$PRAHARI_MIGRATOR_USER" "PRAHARI_MIGRATOR_USER"
require_identifier "$PRAHARI_APP_USER" "PRAHARI_APP_USER"

bootstrap_password="$(read_password_file "$POSTGRES_BOOTSTRAP_PASSWORD_FILE")"
pgpass_file="/tmp/.pgpass"
pgpass_password="$(printf '%s' "$bootstrap_password" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g')"
umask 077
printf '%s:%s:%s:%s:%s\n' \
  "${PGHOST:-postgres}" "${PGPORT:-5432}" "$PRAHARI_DATABASE_NAME" \
  "$POSTGRES_BOOTSTRAP_USER" "$pgpass_password" > "$pgpass_file"
unset bootstrap_password pgpass_password
export PGPASSFILE="$pgpass_file"
trap 'rm -f "$pgpass_file"' EXIT HUP INT TERM

psql \
  --host "${PGHOST:-postgres}" \
  --port "${PGPORT:-5432}" \
  --username "$POSTGRES_BOOTSTRAP_USER" \
  --dbname "$PRAHARI_DATABASE_NAME" \
  --set ON_ERROR_STOP=1 \
  --set database_name="$PRAHARI_DATABASE_NAME" \
  --set app_role="$PRAHARI_APP_USER" <<'SQL'
DO $$
BEGIN
  IF to_regclass('public.telemetry_records') IS NULL
     OR to_regclass('public.app_probe_records') IS NULL
     OR to_regclass('public.event_records') IS NULL
     OR to_regclass('public.incident_states') IS NULL
     OR to_regclass('public.closure_cases') IS NULL
     OR to_regclass('public.action_operations') IS NULL
     OR to_regclass('public.browser_sessions') IS NULL
     OR to_regclass('public.login_windows') IS NULL
     OR to_regclass('public.audit_events') IS NULL
     OR to_regclass('public.alembic_version') IS NULL THEN
    RAISE EXCEPTION 'expected PRAHARI migration tables are missing';
  END IF;
END
$$;

REVOKE ALL PRIVILEGES ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO :"app_role";
REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app_role";

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM :"app_role";
REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM :"app_role";

GRANT SELECT, INSERT, UPDATE ON TABLE
  telemetry_records,
  app_probe_records,
  event_records,
  incident_states,
  closure_cases,
  action_operations
TO :"app_role";

GRANT SELECT, INSERT ON TABLE audit_events TO :"app_role";
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE browser_sessions TO :"app_role";
GRANT SELECT, INSERT, UPDATE ON TABLE login_windows TO :"app_role";
GRANT SELECT ON TABLE alembic_version TO :"app_role";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"app_role";
SQL

printf '%s\n' 'Applied least-privilege runtime grants; audit_events has no UPDATE or DELETE grant.'
