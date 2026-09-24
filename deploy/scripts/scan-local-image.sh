#!/usr/bin/env bash
# Scan one local candidate image by immutable image ID without mounting Docker into the worker.
set -euo pipefail

state="$(realpath -e -- "${1:?prepared owned state required}")"
image_ref="${2:?local image reference required}"
report_name="${3:?bounded report name required}"
[[ "$EUID" == 0 && ! -L "$state/pilot-manifest.json" ]]
[[ "$image_ref" =~ ^[a-zA-Z0-9./:@_-]+$ ]]
[[ "$report_name" =~ ^[a-z0-9][a-z0-9-]{0,79}$ ]]

scan="$state/security-scan-20260914"
tool="$scan/tool/trivy"
[[ -f "$tool" && ! -L "$tool" ]]
database="$scan/cache/db/trivy.db"
database_metadata="$scan/cache/db/metadata.json"
[[ -f "$database" && ! -L "$database" ]]
[[ -f "$database_metadata" && ! -L "$database_metadata" ]]
available_kib="$(df -Pk "$state" | awk 'NR == 2 {print $4}')"
[[ "$available_kib" =~ ^[0-9]+$ && "$available_kib" -ge 20971520 ]] || {
  printf 'ERROR: candidate scanning requires at least 20 GiB free on the owned-state filesystem\n' >&2
  exit 1
}
python3 - "$scan/tool" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
metadata = json.loads((root / "verification.json").read_bytes())
assert metadata["version"] == "0.74.0"
assert metadata["archive_sha256"] == "2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a"
assert hashlib.sha256((root / "trivy.tar.gz").read_bytes()).hexdigest() == metadata["archive_sha256"]
PY

image_id="$(docker image inspect "$image_ref" | jq -er '.[0].Id')"
[[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]]
output="$scan/candidates/$report_name"
[[ ! -e "$output" ]]
install -d -m 0700 -o 10001 -g 10001 "$output" "$output/scratch"
umask 077
docker save --output "$output/image.tar" "$image_id" </dev/null
chmod 0444 "$output/image.tar"

worker="$(jq -er '.images.api' "$state/pilot-manifest.json")"
container=""
cleanup() {
  if [[ -n "$container" && "$(docker inspect -f '{{ index .Config.Labels "prahari.owned.security-scan" }}' "$container" 2>/dev/null)" == true ]]; then
    docker rm -f "$container" >/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_trivy() {
  local format="$1" destination="$2"
  container="prahari-owned-candidate-scan-$(date +%s)-${RANDOM}"
  timeout --kill-after=30s 20m docker run --name "$container" \
    --label prahari.owned.security-scan=true \
    --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --user 10001:10001 --memory 1536m --cpus 0.5 --pids-limit 128 \
    --env GOMEMLIMIT=1GiB \
    --mount "type=bind,src=$output/scratch,dst=/tmp" \
    --mount "type=bind,src=$tool,dst=/scanner/trivy,readonly" \
    --mount "type=bind,src=$output/image.tar,dst=/input/image.tar,readonly" \
    --mount "type=bind,src=$output,dst=/scan" \
    --mount "type=bind,src=$scan/cache,dst=/cache" \
    --entrypoint /scanner/trivy "$worker" image --input /input/image.tar \
    --cache-dir /cache --parallel 1 --scanners vuln --offline-scan \
    --skip-db-update --skip-java-db-update --skip-check-update --format "$format" \
    --output "/scan/$destination" </dev/null
  cleanup
  container=""
}

run_trivy json report.json
run_trivy cyclonedx sbom.cdx.json
trap - EXIT

python3 - "$state" "$output" "$image_ref" "$image_id" <<'PY'
import collections
import datetime
import hashlib
import json
import pathlib
import sys

state, output = map(pathlib.Path, sys.argv[1:3])
image_ref, image_id = sys.argv[3:5]
database_path = state / "security-scan-20260914/cache/db/trivy.db"
database_metadata_path = state / "security-scan-20260914/cache/db/metadata.json"
database_metadata = json.loads(database_metadata_path.read_bytes())
if set(database_metadata) != {"Version", "NextUpdate", "UpdatedAt", "DownloadedAt"}:
    raise ValueError("scanner vulnerability database metadata is invalid")
if database_metadata["Version"] != 2 or any(
    not isinstance(database_metadata[field], str) or not database_metadata[field]
    for field in ("NextUpdate", "UpdatedAt", "DownloadedAt")
):
    raise ValueError("scanner vulnerability database metadata values are invalid")
report_path = output / "report.json"
report = json.loads(report_path.read_bytes())
sbom_path = output / "sbom.cdx.json"
sbom = json.loads(sbom_path.read_bytes())
if sbom.get("bomFormat") != "CycloneDX" or not isinstance(sbom.get("components"), list):
    raise ValueError("scanner did not produce a valid CycloneDX component inventory")
counts = collections.Counter(
    vulnerability["Severity"]
    for result in report.get("Results", [])
    for vulnerability in result.get("Vulnerabilities", [])
)
verification = {
    "schema_version": "owned-candidate-container-scan-v1",
    "status": "scan_completed",
    "completed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "source_reference": image_ref,
    "immutable_local_image_id": image_id,
    "image_archive_sha256": hashlib.sha256((output / "image.tar").read_bytes()).hexdigest(),
    "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
    "sbom_sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest(),
    "sbom_component_count": len(sbom["components"]),
    "findings_by_severity": {
        severity: counts.get(severity, 0)
        for severity in ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")
    },
    "tool": json.loads((state / "security-scan-20260914/tool/verification.json").read_bytes()),
    "vulnerability_database": {
        "sha256": hashlib.sha256(database_path.read_bytes()).hexdigest(),
        "metadata_sha256": hashlib.sha256(database_metadata_path.read_bytes()).hexdigest(),
        "schema_version": database_metadata["Version"],
        "updated_at": database_metadata["UpdatedAt"],
        "downloaded_at": database_metadata["DownloadedAt"],
    },
    "scanner_docker_socket_mounted": False,
    "scanner_network_enabled": False,
    "application_secrets_mounted": False,
    "independent_security_review": False,
    "release_approved": False,
}
(output / "verification.json").write_text(json.dumps(verification, indent=2) + "\n")
print(json.dumps(verification["findings_by_severity"], sort_keys=True))
PY
