#!/usr/bin/env bash
# Collect fixed router interface/OSPF metrics, spool first, then forward.
set -euo pipefail
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_dir="$(cd -- "$script_dir/.." && pwd)"
state_dir="$lab_dir/state/router-collector"
spool_dir="$state_dir/spool"
api_url="http://172.31.255.18:8080"
mkdir -p -- "$spool_dir"
command -v docker >/dev/null
command -v jq >/dev/null
command -v curl >/dev/null
exec 9>"/tmp/prahari-router-collector.lock"
flock -n 9 || exit 0

timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
now_epoch="$(date -u +%s)"
records='[]'

add_record() {
  local metric="$1" value="$2" unit="$3" node="$4" interface="$5" reset="$6"
  records="$(jq -c --arg at "$timestamp" --arg metric "$metric" --arg unit "$unit" \
    --arg node "$node" --arg interface "$interface" --argjson value "$value" \
    --argjson reset "$reset" '. + [{schema_version:"1.0",record_type:"metric",observed_at:$at,source:"containerlab-router",entity_id:"tunnel:branch-01-to-dc-primary",entity_type:"path",metric:$metric,value:$value,unit:$unit,labels:{site_id:"site:branch-01",path_id:"path:branch-01-to-dc:primary",node:$node,interface:$interface},topology_version:"lab-v1",config_version:"lab-config-v1",quality:{source_healthy:true,late_by_ms:0,is_estimated:false,counter_reset:$reset}}]' <<<"$records")"
}

collect_interface() {
  local node="$1" interface="$2" key="${1}-${2}" current previous elapsed reset=false
  current="$(docker exec "clab-prahari-${node}" ip -j -s link show dev "$interface")"
  [[ "$(jq -r '.[0].operstate' <<<"$current")" == "UP" ]] || return 1
  current="$(jq -c --argjson at "$now_epoch" '{at:$at,rx_bytes:.[0].stats64.rx.bytes,rx_packets:.[0].stats64.rx.packets,rx_errors:.[0].stats64.rx.errors,tx_bytes:.[0].stats64.tx.bytes,tx_packets:.[0].stats64.tx.packets,tx_dropped:.[0].stats64.tx.dropped}' <<<"$current")"
  if [[ -s "$state_dir/${key}.json" ]]; then
    previous="$(<"$state_dir/${key}.json")"
    elapsed="$(( now_epoch - $(jq -r '.at' <<<"$previous") ))"
    if (( elapsed > 0 && elapsed <= 300 )); then
      for field in rx_bytes rx_packets rx_errors tx_bytes tx_packets tx_dropped; do
        (( $(jq -r ".$field" <<<"$current") >= $(jq -r ".$field" <<<"$previous") )) || reset=true
      done
      if [[ "$reset" == false ]]; then
        rx_bps="$(awk -v n="$(jq -r '.rx_bytes' <<<"$current")" -v o="$(jq -r '.rx_bytes' <<<"$previous")" -v s="$elapsed" 'BEGIN { printf "%.6f", (n-o)*8/s }')"
        tx_bps="$(awk -v n="$(jq -r '.tx_bytes' <<<"$current")" -v o="$(jq -r '.tx_bytes' <<<"$previous")" -v s="$elapsed" 'BEGIN { printf "%.6f", (n-o)*8/s }')"
        rx_error_ratio="$(awk -v n="$(jq -r '.rx_errors' <<<"$current")" -v o="$(jq -r '.rx_errors' <<<"$previous")" -v p="$(($(jq -r '.rx_packets' <<<"$current")-$(jq -r '.rx_packets' <<<"$previous")))" 'BEGIN { printf "%.9f", (p > 0 ? (n-o)/p : 0) }')"
        tx_drop_ratio="$(awk -v n="$(jq -r '.tx_dropped' <<<"$current")" -v o="$(jq -r '.tx_dropped' <<<"$previous")" -v p="$(($(jq -r '.tx_packets' <<<"$current")-$(jq -r '.tx_packets' <<<"$previous")))" 'BEGIN { printf "%.9f", (p > 0 ? (n-o)/p : 0) }')"
        add_record interface_rx_bps "$rx_bps" bps "$node" "$interface" false
        add_record interface_tx_bps "$tx_bps" bps "$node" "$interface" false
        add_record interface_rx_error_ratio "$rx_error_ratio" ratio "$node" "$interface" false
        add_record interface_tx_drop_ratio "$tx_drop_ratio" ratio "$node" "$interface" false
      fi
    fi
  fi
  tmp="$state_dir/.${key}.$$"
  printf '%s\n' "$current" > "$tmp"
  mv -f -- "$tmp" "$state_dir/${key}.json"
}

collect_interface branch-edge eth1
collect_interface dc-edge eth1
neighbors="$(docker exec clab-prahari-branch-edge vtysh -c 'show ip ospf neighbor json')"
full_neighbors="$(jq '[.neighbors[][] | select(.converged == "Full")] | length' <<<"$neighbors")"
add_record ospf_full_neighbors "$full_neighbors" count branch-edge all false

payload="$(jq -cn --arg run_id "router-${now_epoch}" --argjson records "$records" '{run_id:$run_id,records:$records}')"
count="$(jq '.records | length' <<<"$payload")"
(( count > 0 )) || exit 0
(( $(find "$spool_dir" -maxdepth 1 -type f -name '*.json' | wc -l) < 10000 )) || { printf 'ERROR: router telemetry spool is full\n' >&2; exit 1; }
batch_id="$(printf '%s' "$payload" | sha256sum | cut -d' ' -f1)"
tmp="$spool_dir/.${batch_id}.tmp"
printf '%s\n' "$payload" > "$tmp"
mv -f -- "$tmp" "$spool_dir/${batch_id}.json"

for batch in "$spool_dir"/*.json; do
  [[ -e "$batch" ]] || break
  expected="$(jq '.records | length' "$batch")"
  response="$(curl --fail --silent --show-error --max-time 10 -H 'Authorization: Bearer lab-router-collector' -H 'X-Actor-Role: collector' -H 'Content-Type: application/json' --data-binary "@$batch" "$api_url/api/telemetry/metrics")" || exit 1
  [[ "$(jq -r '.accepted' <<<"$response")" == "$expected" ]] || { printf 'ERROR: acknowledgement mismatch\n' >&2; exit 1; }
  rm -f -- "$batch"
done
printf 'Forwarded %s router metrics at %s.\n' "$count" "$timestamp"
