#!/usr/bin/env bash
# Restore a sealed PRAHARI backup into an isolated hardened PostgreSQL candidate.
set -euo pipefail

image_ref="${1:?candidate image reference required}"
backup_dir="$(realpath -e -- "${2:?backup directory required}")"
[[ "$EUID" == 0 && "$image_ref" =~ ^[a-zA-Z0-9./:@_-]+$ ]]
[[ -d "$backup_dir" && ! -L "$backup_dir" ]]
[[ "$(realpath -se -- "$2")" == "$backup_dir" ]]
for file in database.dump database.sha256 snapshot.json; do
  [[ -f "$backup_dir/$file" && ! -L "$backup_dir/$file" ]]
done
expected_archive="$(tr -d '[:space:]' < "$backup_dir/database.sha256")"
[[ "$expected_archive" =~ ^[a-f0-9]{64}$ ]]
[[ "$(sha256sum "$backup_dir/database.dump" | awk '{print $1}')" == "$expected_archive" ]]

image_id="$(docker image inspect "$image_ref" | jq -er '.[0].Id')"
[[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]]
[[ "$(docker image inspect "$image_id" | jq -er '.[0].Config.User')" == "70:70" ]]
suffix="$(date +%s)-${RANDOM}"
container="prahari-postgres-restore-$suffix"
volume="prahari-postgres-restore-$suffix"
password="$(cat /proc/sys/kernel/random/uuid)"
started_at="$SECONDS"
staging="$(mktemp -d /tmp/prahari-postgres-restore.XXXXXXXX)"
install -o 70 -g 70 -m 0444 "$backup_dir/database.dump" "$staging/database.dump"
chmod 0555 "$staging"
cleanup() {
  if [[ "$(docker inspect -f '{{ index .Config.Labels "prahari.postgres.restore-verify" }}' "$container" 2>/dev/null)" == true ]]; then
    docker rm -f "$container" >/dev/null || true
  fi
  if [[ "$(docker volume inspect -f '{{ index .Labels "prahari.postgres.restore-verify" }}' "$volume" 2>/dev/null)" == true ]]; then
    docker volume rm "$volume" >/dev/null || true
  fi
  rm -f -- "$staging/database.dump"
  rmdir -- "$staging" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker volume create --label prahari.postgres.restore-verify=true "$volume" >/dev/null
docker run -d --name "$container" --label prahari.postgres.restore-verify=true \
  --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 70:70 --memory 768m --cpus 1 --pids-limit 128 \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m,uid=70,gid=70 \
  --tmpfs /var/run/postgresql:rw,noexec,nosuid,size=16m,uid=70,gid=70 \
  --mount "type=volume,src=$volume,dst=/var/lib/postgresql/data" \
  --mount "type=bind,src=$staging,dst=/backup,readonly" \
  --env POSTGRES_PASSWORD="$password" "$image_id" >/dev/null
for _ in $(seq 1 90); do
  docker exec "$container" pg_isready -U postgres -d postgres >/dev/null 2>&1 && break
  [[ "$(docker inspect -f '{{.State.Running}}' "$container")" == true ]] || {
    docker logs "$container" >&2
    exit 1
  }
  sleep 1
done
docker exec "$container" pg_isready -U postgres -d postgres >/dev/null
docker exec "$container" createdb -U postgres restore_check
docker exec "$container" pg_restore -U postgres -d restore_check \
  --exit-on-error --single-transaction --no-owner --no-acl /backup/database.dump
revision="$(docker exec "$container" psql -At -v ON_ERROR_STOP=1 -U postgres -d restore_check \
  -c 'SELECT version_num FROM alembic_version;')"
counts="$(docker exec "$container" psql -At -v ON_ERROR_STOP=1 -U postgres -d restore_check \
  -c "SELECT json_build_object(
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
  )::text;")"
python3 - "$backup_dir/snapshot.json" "$image_id" "$expected_archive" "$revision" "$counts" "$((SECONDS - started_at))" <<'PY'
import json
import pathlib
import sys

snapshot_path = pathlib.Path(sys.argv[1])
image_id, archive_sha256, revision, counts_json, elapsed = sys.argv[2:]
snapshot = json.loads(snapshot_path.read_bytes())
counts = json.loads(counts_json)
if revision != snapshot["alembic_revision"] or counts != snapshot["table_counts"]:
    raise SystemExit("restored schema revision or table counts differ from the sealed snapshot")
print(
    json.dumps(
        {
            "schema_version": "hardened-postgres-restore-verification-v1",
            "passed": True,
            "immutable_local_image_id": image_id,
            "archive_sha256": archive_sha256,
            "alembic_revision": revision,
            "table_counts": counts,
            "restore_seconds": int(elapsed),
            "restore_network": "none",
            "container_user": "70:70",
            "source_database_switched": False,
        },
        sort_keys=True,
    )
)
PY
