# Telemetry and data contract

## Goal

This contract turns router and application observations into consistent input for prediction. A model is only as trustworthy as the entity identity, timestamp, unit, and topology context attached to every record.

## How PRAHARI collects health information

| Source | Collection method | Examples |
|---|---|---|
| Router and switch counters | SNMP polling or gNMI streaming | Bytes, packets, drops, CRC errors, interface state, CPU, memory. |
| Tunnel/SLA health | SD-WAN API, gNMI, device CLI adapter, or active probes | Loss, RTT, jitter, path state, tunnel flaps, rekey failures. |
| Routing changes | Syslog, BGP/OSPF telemetry, or parser adapter | Neighbor changes, route withdraws, convergence events, path changes. |
| Flow/application load | NetFlow/IPFIX/sFlow and application metrics | Active flows, traffic class, upload duration, video bitrate, chat response time. |
| Configuration and inventory | Versioned local files or CMDB export | Device/site mapping, interface capacity, role, policy, topology version. |

The collector uses read-only credentials where possible. It must not require a cloud service.

## Durable collector forwarding

Vendor adapters should emit normalized JSONL and hand it to the checked-in
collector forwarder. The forwarder validates every record against this contract,
writes each batch to a bounded disk spool before transmission, retries batches in
FIFO order, and removes a batch only after the API acknowledges the exact record
count. Batch identities are deterministic, and the evidence store uses
deterministic record identities, so retry after an uncertain response is
idempotent.

Use a mounted collector-role API-key file; never pass a secret as a command-line
argument. Remote API targets must use HTTPS. Loopback HTTP is accepted for local
development only.

~~~powershell
uv run python scripts/telemetry_collector.py `
  --input artifacts/normalized-router-metrics.jsonl `
  --spool artifacts/collector-spool `
  --run-id site-collection-2026-09-01 `
  --api-url http://127.0.0.1:8000 `
  --api-key-file deploy/secrets/collector_api_key
~~~

Run the same command without `--input` to retry already-spooled batches. A full
spool applies backpressure and exits with an error instead of deleting old
evidence. Source-specific SNMP, gNMI, or vendor API adapters remain separate;
they must map stable identities and units into the normalized records below.

## Sampling and windows

For the MVP:

- collect or derive key numeric metrics every 5 seconds;
- treat samples as stale after two missed cadences, while tolerating an isolated
  scheduler delay up to the configured 15-second inter-sample ceiling;
- create a rolling 60-second feature window every 5 seconds;
- predict at 30, 60, and 120 second horizons;
- retain raw data and derived features with a run ID for training and audit.

Production deployments can use longer windows or different sampling rates, but the values must be versioned with the model.

## Canonical entity identifiers

Every observation must be traceable to a stable entity:

| Entity type | Example ID |
|---|---|
| Device | device:branch-01-edge |
| Interface | interface:branch-01-edge:wan-primary |
| Tunnel | tunnel:branch-01-to-dc-primary |
| Path | path:branch-01-to-dc:primary |
| Site | site:branch-01 |
| Application probe | app:video:branch-01-to-dc |

The inventory maps IDs to friendly names, physical site, role, capacity, upstream/downstream relationships, and current topology/configuration versions.

## Normalized records

### Metric record

~~~json
{
  "schema_version": "1.0",
  "record_type": "metric",
  "observed_at": "2026-08-13T10:00:05Z",
  "source": "gnmi",
  "entity_id": "tunnel:branch-01-to-dc-primary",
  "entity_type": "tunnel",
  "metric": "packet_loss_ratio",
  "value": 0.032,
  "unit": "ratio",
  "labels": {
    "site_id": "site:branch-01",
    "path_id": "path:branch-01-to-dc:primary",
    "traffic_class": "video"
  },
  "topology_version": "lab-v1",
  "config_version": "branch-01-17",
  "quality": {
    "source_healthy": true,
    "late_by_ms": 40,
    "is_estimated": false
  }
}
~~~

### Event record

~~~json
{
  "schema_version": "1.0",
  "record_type": "event",
  "observed_at": "2026-08-13T10:00:10Z",
  "source": "syslog",
  "entity_id": "device:branch-01-edge",
  "event_type": "tunnel_state_change",
  "severity": "warning",
  "message": "Primary tunnel probe latency increased",
  "related_entity_ids": [
    "tunnel:branch-01-to-dc-primary",
    "path:branch-01-to-dc:primary"
  ],
  "topology_version": "lab-v1",
  "quality": {
    "source_healthy": true
  }
}
~~~

## Derived signals

Never train directly on monotonically increasing raw byte/packet counters. Convert counters into rates and create features such as:

- utilization percentage;
- packet loss and error rates;
- RTT/latency and jitter statistics;
- queue/drop rate;
- event counts per window;
- latest value, slope, moving average, p95, standard deviation;
- EWMA deviation and rolling z-score;
- recent configuration-change indicator;
- neighboring path anomaly score;
- application-impact signals, such as upload duration or video bitrate.

## Data quality rules

The feature service must attach a data-quality status. It should withhold or downgrade predictions when:

- a source is stale or missing;
- timestamps are outside the accepted clock skew;
- counters reset unexpectedly;
- the entity is not in the current inventory/topology;
- the feature window has too many missing samples;
- the configuration/topology version is unknown.

The dashboard must show this state. A confident prediction made from missing or mismatched data is not acceptable.

## Storage boundary

Store numeric metrics, events, inventory, topology, raw fault manifests, feature sets, predictions, and feedback locally. The MVP can use a simple local time-series store plus structured files/database; the storage technology is selected in [tech-stack.md](tech-stack.md).
