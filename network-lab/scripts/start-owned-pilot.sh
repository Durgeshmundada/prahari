#!/usr/bin/env bash
# Start only the prepared owned pilot. Existing prahari resources are untouched.
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
state_dir="${1:?Usage: start-owned-pilot.sh /absolute/prepared/state [cached-model-volume]}"
state_dir="$(realpath -e -- "$state_dir")"
[[ -f "$state_dir/pilot-manifest.json" && ! -L "$state_dir/pilot-manifest.json" ]] || exit 1
jq -e '.schema_version == "owned-pilot-v1" and .lab == "prahari-owned" and .action_adapter_enabled == false' \
  "$state_dir/pilot-manifest.json" >/dev/null
for tool in docker containerlab jq curl; do command -v "$tool" >/dev/null; done
# Reject changed router configuration before deploying an allegedly identical
# logical topology. Physical names and management networking are isolated.
python3 - "$state_dir/pilot-manifest.json" "$repo_dir" <<'PY'
import hashlib,json,pathlib,sys
m=json.loads(pathlib.Path(sys.argv[1]).read_text())
runtime=pathlib.Path(sys.argv[1]).parent/'runtime'
if m['runtime_root']!=str(runtime): raise SystemExit('Unexpected pilot runtime path.')
for name,digest in m['runtime_files'].items():
    path=runtime/name
    if path.is_symlink() or not path.resolve().is_relative_to(runtime.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
        raise SystemExit('Frozen pilot runtime changed; refusing deployment.')
for name,digest in m['artifacts'].items():
    if hashlib.sha256((pathlib.Path(sys.argv[1]).parent/name).read_bytes()).hexdigest()!=digest:
        raise SystemExit('Prepared pilot configuration changed; refusing deployment.')
for name,digest in m['router_configs'].items():
    if hashlib.sha256((runtime/name).read_bytes()).hexdigest()!=digest:
        raise SystemExit('Router configuration changed; prepare a new pilot identity.')
PY
if [[ "$EUID" == 0 ]]; then
  install -d -m 700 -o 10001 -g 10001 "$state_dir/spool"
else
  sudo -n install -d -m 700 -o 10001 -g 10001 "$state_dir/spool"
fi
for image in $(jq -r '.images[]' "$state_dir/pilot-manifest.json"); do
  docker image inspect "$image" >/dev/null
done
needs_reconfigure=0
for node in branch-client app-server branch-edge pe-primary core pe-secondary pe-backup dc-edge; do
  container="clab-prahari-owned-$node"
  if docker inspect "$container" >/dev/null 2>&1; then
    [[ "$(docker inspect -f '{{ index .Config.Labels "containerlab" }}' "$container")" == prahari-owned ]] \
      || { printf 'Refusing a foreign container using a pilot name.\n' >&2; exit 1; }
    docker exec "$container" ip link show eth1 >/dev/null 2>&1 || needs_reconfigure=1
  else
    needs_reconfigure=1
  fi
done
if ! docker inspect clab-prahari-owned-branch-client >/dev/null 2>&1; then
  # A fresh pilot must not steal a subnet from any existing Docker network.
  docker network ls -q | xargs -r docker network inspect | python3 -c '
import ipaddress,json,sys
target=ipaddress.ip_network("172.30.254.0/24")
for n in json.load(sys.stdin):
 for c in n.get("IPAM",{}).get("Config",[]) or []:
  subnet=c.get("Subnet")
  if subnet and ":" not in subnet and target.overlaps(ipaddress.ip_network(subnet)):
   raise SystemExit("Pilot subnet overlaps an existing Docker network.")'
  if [[ "$EUID" == 0 ]]; then
    containerlab deploy -t "$state_dir/topology.clab.json"
  else
    sudo -n containerlab deploy -t "$state_dir/topology.clab.json"
  fi
elif (( needs_reconfigure == 1 )); then
  # Docker restarts preserve containers but lose Containerlab veth links.
  # Recreate only the checksum-bound owned topology, never database volumes.
  if [[ "$EUID" == 0 ]]; then
    containerlab deploy --reconfigure -t "$state_dir/topology.clab.json"
  else
    sudo -n containerlab deploy --reconfigure -t "$state_dir/topology.clab.json"
  fi
fi
for node in branch-client app-server branch-edge pe-primary core pe-secondary pe-backup dc-edge; do
  [[ "$(docker inspect -f '{{ index .Config.Labels "containerlab" }}' "clab-prahari-owned-$node")" == prahari-owned ]]
  docker exec "clab-prahari-owned-$node" ip link show eth1 >/dev/null
done
compose=(docker compose --env-file "$state_dir/pilot.env" -p prahari-owned
  -f "$repo_dir/deploy/compose.yaml" -f "$state_dir/compose.pilot.json")
"${compose[@]}" config --quiet
"${compose[@]}" create ollama
if [[ -n "${2:-}" ]]; then
  # A read-only copy of cached weights seeds our own volume. Never write the
  # original AI server's volume or manufacture a model acceptance record.
  docker volume inspect "$2" >/dev/null
  ollama_image="$(jq -r '.images.ollama' "$state_dir/pilot-manifest.json")"
  docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges \
    --mount "type=volume,src=$2,dst=/source,readonly" \
    --mount type=volume,src=prahari-owned_ollama_models,dst=/destination \
    --entrypoint /bin/sh "$ollama_image" -c \
    'test ! -e /destination/models && cp -a /source/models /destination/models'
fi
"${compose[@]}" up -d --wait --wait-timeout 180
if ! docker exec clab-prahari-owned-app-server pgrep -f 'traffic_demo_receiver.py' >/dev/null; then
docker exec -d clab-prahari-owned-app-server sh -c \
  'export PRAHARI_TRAFFIC_DEMO_TOKEN="$(cat /run/prahari-traffic-token)"; exec /opt/venv/bin/python scripts/traffic_demo_receiver.py --host 172.16.20.2 --port 8765 --allow-non-loopback > /tmp/prahari-owned-receiver.log 2>&1'
fi
for attempt in $(seq 1 30); do
  if docker exec clab-prahari-owned-app-server curl --fail --silent --max-time 2 \
    http://172.16.20.2:8765/health >/dev/null; then break; fi
  sleep 1
done
docker exec clab-prahari-owned-app-server curl --fail --silent --max-time 2 \
  http://172.16.20.2:8765/health >/dev/null
printf 'Owned pilot started. Browser credentials: %s/credentials.txt\n' "$state_dir"
