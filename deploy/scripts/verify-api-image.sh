#!/usr/bin/env bash
# Verify the shell-free API candidate without application secrets or network.
set -euo pipefail

image="${1:?local API candidate reference required}"
report="${2:-artifacts/security-check/owned-containers/candidate-api-distroless-runtime.json}"
image_id="$(docker image inspect --format '{{.Id}}' "$image")"
[[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]]
configured_user="$(docker image inspect --format '{{.Config.User}}' "$image")"
[[ "$configured_user" == "10001:10001" ]]

runtime_json="$(docker run --rm --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
  --cap-drop ALL --security-opt no-new-privileges --user 10001:10001 \
  --memory 1g --cpus 1 --pids-limit 128 \
  --entrypoint /opt/venv/bin/python "$image" -c '
import errno, json, os
import alembic, lightgbm, numpy, prahari.api, sqlalchemy

write_blocked = False
try:
    open("/app/runtime-write-must-fail", "wb").close()
except OSError as error:
    write_blocked = error.errno in {errno.EACCES, errno.EROFS}
assert write_blocked
assert os.getuid() == 10001 and os.getgid() == 10001
print(json.dumps({
    "uid": os.getuid(),
    "gid": os.getgid(),
    "python": os.sys.version.split()[0],
    "lightgbm": lightgbm.__version__,
    "numpy": numpy.__version__,
    "sqlalchemy": sqlalchemy.__version__,
    "root_filesystem_write_blocked": write_blocked,
    "api_imported": True,
}, sort_keys=True))
')"

docker run --rm --network none --read-only --tmpfs /tmp:rw,noexec,nosuid,nodev,size=32m \
  --cap-drop ALL --security-opt no-new-privileges --user 10001:10001 \
  --memory 512m --cpus 0.5 --pids-limit 64 \
  --entrypoint /opt/venv/bin/alembic "$image" --help >/dev/null

if docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --entrypoint /bin/sh "$image" -c true >/dev/null 2>&1; then
  printf 'ERROR: API candidate unexpectedly contains an executable /bin/sh\n' >&2
  exit 1
fi

mkdir -p -- "$(dirname -- "$report")"
temporary="${report}.tmp.$$"
trap 'rm -f -- "$temporary"' EXIT
python3 - "$image" "$image_id" "$configured_user" "$runtime_json" "$temporary" <<'PY'
import datetime
import json
import pathlib
import sys

image, image_id, configured_user, runtime_json, output = sys.argv[1:]
report = {
    "schema_version": "prahari-api-runtime-verification-v1",
    "status": "passed",
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source_reference": image,
    "immutable_local_image_id": image_id,
    "configured_user": configured_user,
    "runtime": json.loads(runtime_json),
    "network_enabled": False,
    "application_secrets_mounted": False,
    "linux_capabilities_dropped": True,
    "no_new_privileges": True,
    "shell_present": False,
    "model_approved": False,
    "release_approved": False,
}
pathlib.Path(output).write_text(json.dumps(report, indent=2) + "\n")
PY
mv -- "$temporary" "$report"
trap - EXIT
printf 'Distroless API runtime verified: %s\n' "$report"
