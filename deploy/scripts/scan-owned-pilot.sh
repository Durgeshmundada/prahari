#!/usr/bin/env bash
# Scan immutable owned images in an isolated worker without a Docker socket.
set -euo pipefail
state="$(realpath -e -- "${1:?prepared owned state required}")"
[[ "$EUID" == 0 && ! -L "$state/pilot-manifest.json" ]]
jq -e '.lab == "prahari-owned" and .action_adapter_enabled == false' "$state/pilot-manifest.json" >/dev/null
scan="$state/security-scan-20260914"
components=("${@:2}")
if (( ${#components[@]} == 0 )); then components=(api dashboard postgres ollama); fi
for component in "${components[@]}"; do
  case "$component" in api|dashboard|postgres|ollama) ;; *) exit 1 ;; esac
done
install -d -m 700 -o 10001 -g 10001 "$scan/scratch"
[[ -f "$scan/tool/trivy" && ! -L "$scan/tool/trivy" ]]
python3 - "$scan/tool" <<'PY'
import hashlib,json,pathlib,sys
p=pathlib.Path(sys.argv[1]);metadata=json.loads((p/'verification.json').read_bytes())
assert metadata['version']=='0.74.0'
assert metadata['archive_sha256']=='2ae6fe3ee734b7fdf11335663e18c75ea12dccc76062f09f164a3b0f8be4371a'
assert hashlib.sha256((p/'trivy.tar.gz').read_bytes()).hexdigest()==metadata['archive_sha256']
PY
umask 077
name=""
cleanup() {
  if [[ -n "$name" && "$(docker inspect -f '{{ index .Config.Labels "prahari.owned.security-scan" }}' "$name" 2>/dev/null)" == true ]]; then
    docker rm -f "$name" >/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
worker="$(jq -r '.images.api' "$state/pilot-manifest.json")"
for component in "${components[@]}"; do
  image="$(jq -r --arg component "$component" '.images[$component]' "$state/pilot-manifest.json")"
  docker image inspect "$image" >/dev/null
  timeout --kill-after=30s 20m docker save --output "$scan/inputs/$component.tar" "$image" </dev/null
  chmod 444 "$scan/inputs/$component.tar"
  name="prahari-owned-security-${component}-$(date +%s)-${RANDOM}"
  timeout --kill-after=30s 20m docker run --name "$name" --label prahari.owned.security-scan=true \
    --network bridge --read-only --cap-drop ALL --security-opt no-new-privileges --user 10001:10001 \
    --memory 1536m --cpus 0.5 --pids-limit 128 --env GOMEMLIMIT=1GiB \
    --mount "type=bind,src=$scan/scratch,dst=/tmp" \
    --mount "type=bind,src=$scan/tool/trivy,dst=/scanner/trivy,readonly" \
    --mount "type=bind,src=$scan/inputs,dst=/inputs,readonly" \
    --mount "type=bind,src=$scan/output,dst=/output" \
    --mount "type=bind,src=$scan/cache,dst=/cache" \
    --entrypoint /scanner/trivy "$worker" image --input "/inputs/$component.tar" \
    --cache-dir /cache --parallel 1 --scanners vuln --offline-scan --skip-java-db-update --skip-check-update \
    --format json --output "/output/$component.json" </dev/null
  cleanup
  name=""
  printf 'Owned immutable image scanned: %s\n' "$component"
done
python3 - "$state" "$scan" <<'PY'
import collections,datetime,hashlib,json,pathlib,sys
state,root=map(pathlib.Path,sys.argv[1:]);pilot=json.loads((state/'pilot-manifest.json').read_bytes());images={}
for component in ('api','dashboard','postgres','ollama'):
 p=root/'output'/f'{component}.json';r=json.loads(p.read_bytes());counts=collections.Counter(v['Severity'] for result in r.get('Results',[]) for v in result.get('Vulnerabilities',[]))
 images[component]={'image':pilot['images'][component],'report_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'findings_by_severity':dict(counts)}
report={'schema_version':'owned-container-scan-v1','status':'scan_completed','completed_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'tool':json.loads((root/'tool/verification.json').read_bytes()),'images':images,'pilot_sha256':hashlib.sha256((state/'pilot-manifest.json').read_bytes()).hexdigest(),'scanner_docker_socket_mounted':False,'application_secrets_mounted':False,'independent_security_review':False,'release_approved':False}
(root/'output/summary.json').write_text(json.dumps(report,indent=2)+'\n')
print('Container scan reports complete; findings still require review.')
PY
