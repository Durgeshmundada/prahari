#!/usr/bin/env bash
# Validate the Compose model, image references, and secret-file layout. This
# script does not build, pull, start, stop, or remove containers, networks,
# images, or volumes. It never prints secret contents. For a real environment,
# image inspection is read-only and proves that every exact digest is preloaded.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd -- "$script_dir/.." && pwd)"
env_file="${1:-$deploy_dir/.env.example}"
template_env=0
production_env=0
postgres_secret_uid=70
runtime_secret_uid=10001

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
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
  [[ "${value:0:1}" != '"' && "${value:0:1}" != "'" ]] \
    || fail "$key must be an unquoted path/identifier in $env_file"
  printf '%s' "$value"
}

require_identifier() {
  local value="$1"
  local label="$2"
  [[ "$value" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] \
    || fail "$label must be a lowercase PostgreSQL identifier of at most 63 characters"
}

require_uvicorn_log_level() {
  local value="$1"
  case "$value" in
    critical|error|warning|info|debug|trace) ;;
    *) fail "PRAHARI_UVICORN_LOG_LEVEL must be a lowercase Uvicorn log-level choice" ;;
  esac
}

require_digest_image() {
  local key="$1"
  local reference
  reference="$(read_env_value "$key")"
  [[ "$reference" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] \
    || fail "$key must be an immutable name@sha256:<64 lowercase hex> reference"
  if (( ! template_env )); then
    [[ "${reference##*@sha256:}" != "0000000000000000000000000000000000000000000000000000000000000000" ]] \
      || fail "$key still contains the all-zero template digest"
    docker image inspect "$reference" >/dev/null 2>&1 \
      || fail "$key is not preloaded under its exact digest: $reference"
  fi
}

resolve_secret_path() {
  local configured_path="$1"
  if [[ "$configured_path" = /* ]]; then
    printf '%s' "$configured_path"
  else
    printf '%s' "$deploy_dir/${configured_path#./}"
  fi
}

validate_secret_file() {
  local key="$1"
  local expected_uid="$2"
  local configured_path
  local secret_path
  local mode
  local owner_uid
  local contents

  configured_path="$(read_env_value "$key")"
  secret_path="$(resolve_secret_path "$configured_path")"
  [[ -f "$secret_path" ]] || fail "$key does not point to a regular file: $configured_path"
  [[ ! -L "$secret_path" ]] || fail "$key must not point to a symbolic link: $configured_path"

  # Checked-in templates are safe only for `docker compose config`; they must
  # never be treated as deployable secrets.
  if [[ "$secret_path" == *.example ]]; then
    (( template_env )) || fail "$key points to a checked-in template; use a real secret file in $env_file"
    return
  fi

  # Docker Compose implements file secrets as bind mounts, preserving the host
  # numeric owner. Real validation must therefore run as root against the
  # container-owned 0600 files, rather than as the unprivileged deploy user.
  [[ "$(id -u)" -eq 0 ]] \
    || fail "validate real secret files with sudo; local Compose file secrets use host ownership"
  [[ -r "$secret_path" ]] || fail "$key is not readable by root: $configured_path"

  mode="$(stat -c '%a' "$secret_path")" \
    || fail "could not read permissions for $configured_path"
  owner_uid="$(stat -c '%u' "$secret_path")" \
    || fail "could not read owner for $configured_path"
  [[ "$owner_uid" == "$expected_uid" ]] \
    || fail "$configured_path must be owned by UID $expected_uid for its non-root container reader"
  (( (8#$mode & 8#077) == 0 )) \
    || fail "$configured_path must not be accessible by group or other users (use chmod 600)"
  [[ "$(wc -l < "$secret_path")" -le 1 ]] \
    || fail "$configured_path must contain exactly one line"
  contents="$(tr -d '\r\n' < "$secret_path")"
  [[ -n "$contents" ]] || fail "$configured_path is empty"
  case "$contents" in
    CHANGE_ME*|REPLACE*|EXAMPLE*) fail "$configured_path still contains a placeholder" ;;
  esac
  case "$key" in
    *_PASSWORD_FILE)
      [[ "${#contents}" -ge 24 ]] \
        || fail "$configured_path must contain at least 24 characters"
      ;;
  esac
}

validate_database_url_secret() {
  local key="$1"
  local expected_role="$2"
  local configured_path
  local secret_path
  local database_url

  configured_path="$(read_env_value "$key")"
  secret_path="$(resolve_secret_path "$configured_path")"
  [[ "$secret_path" == *.example ]] && return
  database_url="$(tr -d '\r\n' < "$secret_path")"
  [[ "$database_url" == "postgresql+psycopg://${expected_role}:"* ]] \
    || fail "$configured_path must begin with the expected PostgreSQL role URL"
  [[ "$database_url" == *"@postgres:5432/${database_name}"* ]] \
    || fail "$configured_path must use the internal postgres:5432/${database_name} endpoint"
}

validate_api_keys_json_secret() {
  local configured_path
  local secret_path

  configured_path="$(read_env_value PRAHARI_API_KEYS_JSON_FILE)"
  secret_path="$(resolve_secret_path "$configured_path")"
  [[ "$secret_path" == *.example ]] && return
  command -v python3 >/dev/null 2>&1 || fail "Python 3 is required for JSON validation"
  python3 - "$secret_path" <<'PY' || fail "$configured_path is not a valid API principal map"
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
roles = {"viewer", "operator", "reviewer", "admin", "collector"}
if not isinstance(value, dict) or not value:
    raise SystemExit("API principal map must be a non-empty object")
for token, principal in value.items():
    if not isinstance(token, str) or len(token) < 32 or token.strip() != token:
        raise SystemExit("every API token must be at least 32 characters without edge whitespace")
    if not isinstance(principal, dict) or set(principal) != {"actor_id", "role"}:
        raise SystemExit("every principal must contain exactly actor_id and role")
    if not isinstance(principal["actor_id"], str) or not principal["actor_id"].strip():
        raise SystemExit("actor_id must be a non-empty string")
    if principal["role"] not in roles:
        raise SystemExit("principal role is unsupported")
PY
}

if [[ ! -f "$env_file" ]]; then
  fail "environment file not found: $env_file"
fi

env_file="$(cd -- "$(dirname -- "$env_file")" && pwd)/$(basename -- "$env_file")"
if [[ "$env_file" == *.example ]]; then
  template_env=1
fi
environment="$(read_env_value PRAHARI_ENVIRONMENT)"
if [[ "$environment" == "production" ]]; then
  production_env=1
elif [[ "$environment" != "development" && "$environment" != "test" ]]; then
  fail "PRAHARI_ENVIRONMENT must be development, test, or production"
fi
database_name="$(read_env_value PRAHARI_DATABASE_NAME)"
bootstrap_user="$(read_env_value POSTGRES_BOOTSTRAP_USER)"
migrator_user="$(read_env_value PRAHARI_MIGRATOR_USER)"
app_user="$(read_env_value PRAHARI_APP_USER)"
uvicorn_log_level="$(read_env_value PRAHARI_UVICORN_LOG_LEVEL)"
require_identifier "$database_name" PRAHARI_DATABASE_NAME
require_identifier "$bootstrap_user" POSTGRES_BOOTSTRAP_USER
require_identifier "$migrator_user" PRAHARI_MIGRATOR_USER
require_identifier "$app_user" PRAHARI_APP_USER
require_uvicorn_log_level "$uvicorn_log_level"
[[ "$bootstrap_user" != "$migrator_user" && "$bootstrap_user" != "$app_user" \
  && "$migrator_user" != "$app_user" ]] || fail "bootstrap, migrator, and app roles must differ"

for key in \
  POSTGRES_BOOTSTRAP_PASSWORD_FILE \
  PRAHARI_MIGRATOR_PASSWORD_FILE \
  PRAHARI_APP_PASSWORD_FILE; do
  validate_secret_file "$key" "$postgres_secret_uid"
done
for key in \
  PRAHARI_MIGRATOR_DATABASE_URL_FILE \
  PRAHARI_APP_DATABASE_URL_FILE \
  PRAHARI_API_KEYS_JSON_FILE; do
  validate_secret_file "$key" "$runtime_secret_uid"
done
validate_database_url_secret PRAHARI_MIGRATOR_DATABASE_URL_FILE "$migrator_user"
validate_database_url_secret PRAHARI_APP_DATABASE_URL_FILE "$app_user"
validate_api_keys_json_secret

if ! command -v docker >/dev/null 2>&1; then
  fail "Docker CLI is required to validate the Compose model"
fi
docker compose version >/dev/null

require_digest_image PRAHARI_IMAGE
require_digest_image PRAHARI_DASHBOARD_IMAGE
require_digest_image POSTGRES_IMAGE

compose_files=(-f "$deploy_dir/compose.yaml")
assertion_args=()
if (( production_env )); then
  validate_secret_file PRAHARI_BROWSER_USERS_JSON_FILE "$runtime_secret_uid"
  [[ "$(read_env_value PRAHARI_DATA_MODE)" == "live" ]] \
    || fail "production requires PRAHARI_DATA_MODE=live"
  [[ "$(read_env_value PRAHARI_AUTH_MODE)" == "api_key" ]] \
    || fail "production requires PRAHARI_AUTH_MODE=api_key"
  [[ "$(read_env_value PRAHARI_MODEL_APPROVAL_STATUS)" == "approved" ]] \
    || fail "production requires PRAHARI_MODEL_APPROVAL_STATUS=approved"
  [[ "$(read_env_value PRAHARI_BIND_ADDRESS)" == "127.0.0.1" ]] \
    || fail "this deployment requires loopback binding until a reviewed TLS gateway is added"
  require_digest_image PRAHARI_OLLAMA_IMAGE
  compose_files+=(-f "$deploy_dir/compose.production.yaml")
  assertion_args+=(--production)
  if (( ! template_env )); then
    command -v python3 >/dev/null 2>&1 \
      || fail "Python 3 is required for artifact validation"
    python3 "$script_dir/validate-production-artifacts.py" "$env_file"
  fi
fi

rendered_model="$(mktemp)"
trap 'rm -f "$rendered_model"' EXIT HUP INT TERM
docker compose \
  --project-directory "$deploy_dir" \
  --env-file "$env_file" \
  "${compose_files[@]}" config --format json > "$rendered_model"
command -v python3 >/dev/null 2>&1 \
  || fail "Python 3 is required for rendered-model validation"
python3 "$script_dir/assert-compose-model.py" "${assertion_args[@]}" "$rendered_model"

if (( template_env )); then
  printf 'Template Compose model is structurally valid; placeholder digests and secrets are not deployable.\n'
else
  printf 'Compose configuration, preloaded images, and secret-file layout are valid.\n'
fi
