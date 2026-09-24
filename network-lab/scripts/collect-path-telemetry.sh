#!/usr/bin/env bash
# Collect a continuous end-to-end path sample from the fixed branch client.
set -euo pipefail
sender="clab-prahari-branch-client"
[[ "$(docker inspect --format '{{ index .Config.Labels "containerlab" }}' "$sender" 2>/dev/null)" == "prahari" ]] || exit 1
docker exec "$sender" ip link show eth1 >/dev/null
if ! docker exec "$sender" test -r /run/prahari-lab-api-key; then
  docker exec --user root "$sender" sh -c 'printf %s 00000000000000000000000000000000 > /run/prahari-lab-api-key && chown 10001:10001 /run/prahari-lab-api-key && chmod 600 /run/prahari-lab-api-key'
fi
docker exec -e PYTHONPATH=/app/src "$sender" /opt/venv/bin/python scripts/lab_path_collector.py \
  --run-id continuous-path --phase normal --count 3 \
  --api-url http://172.31.255.18:8080 --api-key-file /run/prahari-lab-api-key
