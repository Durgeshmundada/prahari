#!/bin/sh
# Runs only while the official PostgreSQL image initializes a new data volume.
# It creates distinct bootstrap, migration-owner, and runtime application roles.
# The runtime role receives table privileges only after Alembic finishes, via
# deploy/postgres/grant/02-grant-app-role.sh.
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
  label="$2"
  [ -n "$path" ] || fail "$label file path is empty"
  [ -r "$path" ] || fail "$label file is not readable: $path"
  [ "$(wc -l < "$path")" -le 1 ] || fail "$label must contain one line"
  value="$(tr -d '\r\n' < "$path")"
  [ -n "$value" ] || fail "$label is empty"
  case "$value" in
    CHANGE_ME*|REPLACE*|EXAMPLE*) fail "$label still contains a placeholder" ;;
  esac
  printf '%s' "$value"
}

sql_escape_literal() {
  # PostgreSQL standard string literal escaping. Input arrives on stdin, so the
  # password is never supplied as a command-line argument.
  sed "s/'/''/g"
}

require_identifier "$POSTGRES_USER" "POSTGRES_USER"
require_identifier "$POSTGRES_DB" "POSTGRES_DB"
require_identifier "$PRAHARI_MIGRATOR_USER" "PRAHARI_MIGRATOR_USER"
require_identifier "$PRAHARI_APP_USER" "PRAHARI_APP_USER"

[ "$POSTGRES_USER" != "$PRAHARI_MIGRATOR_USER" ] \
  || fail "bootstrap and migrator roles must differ"
[ "$POSTGRES_USER" != "$PRAHARI_APP_USER" ] \
  || fail "bootstrap and app roles must differ"
[ "$PRAHARI_MIGRATOR_USER" != "$PRAHARI_APP_USER" ] \
  || fail "migrator and app roles must differ"

migrator_password="$(read_password_file "$PRAHARI_MIGRATOR_PASSWORD_FILE" "migrator password")"
app_password="$(read_password_file "$PRAHARI_APP_PASSWORD_FILE" "app password")"
migrator_password_sql="$(printf '%s' "$migrator_password" | sql_escape_literal)"
app_password_sql="$(printf '%s' "$app_password" | sql_escape_literal)"

# POSTGRES_USER is the image bootstrap superuser during first initialization.
# Identifiers are validated above; passwords are SQL-literal escaped before
# entering psql through standard input rather than process arguments.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 <<EOF
CREATE ROLE $PRAHARI_MIGRATOR_USER LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  PASSWORD '$migrator_password_sql';
CREATE ROLE $PRAHARI_APP_USER LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
  PASSWORD '$app_password_sql';

ALTER DATABASE $POSTGRES_DB OWNER TO $PRAHARI_MIGRATOR_USER;
REVOKE ALL PRIVILEGES ON DATABASE $POSTGRES_DB FROM PUBLIC;
GRANT CONNECT ON DATABASE $POSTGRES_DB TO $PRAHARI_MIGRATOR_USER;
GRANT CONNECT ON DATABASE $POSTGRES_DB TO $PRAHARI_APP_USER;

ALTER SCHEMA public OWNER TO $PRAHARI_MIGRATOR_USER;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO $PRAHARI_MIGRATOR_USER;

ALTER DEFAULT PRIVILEGES FOR ROLE $PRAHARI_MIGRATOR_USER IN SCHEMA public
  REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE $PRAHARI_MIGRATOR_USER IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM PUBLIC;
EOF

unset migrator_password app_password migrator_password_sql app_password_sql
printf '%s\n' 'PRAHARI database roles were initialized; runtime grants await successful migration.'
