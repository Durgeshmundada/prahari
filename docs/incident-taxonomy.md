# Incident taxonomy and severity

## Purpose

The taxonomy keeps training labels, dashboard language, RAG queries, and playbooks consistent. Do not train on vague labels such as “network issue.”

## MVP incident classes

| Class | Typical precursor evidence | Location focus | MVP status |
|---|---|---|---|
| Normal | Stable values and no service-impact precursor | N/A | Required |
| Congestion | Rising utilization, queue/drop rate, latency, flow load | Bottleneck interface/path | Later or optional |
| Interface/physical loss | Drops/errors rise without matching offered-load pressure | Device interface/link | Later or optional |
| Tunnel degradation | Rising loss/jitter/RTT, tunnel state events, degraded app probe | Overlay tunnel and underlay path | Required if using overlay |
| Routing instability | BGP/OSPF state changes, path changes, convergence stress | Router/adjacency/path | Later |
| Policy/configuration drift | Configuration diff and correlated behavior change | Policy/device/interface | Later |

For the first demo, label normal and one chosen gradual-degradation class clearly. A smaller honest taxonomy is better than a broad model with no data.

## Evidence-to-class distinctions

| Symptom | Congestion more likely when | Interface/physical loss more likely when | Tunnel degradation more likely when |
|---|---|---|---|
| Packet loss | Utilization/queue pressure rises first | CRC/errors or link health events rise | Loss/jitter occurs on an overlay path and tunnel probes degrade |
| High latency/jitter | Offered load and queue delay rise | Link retransmission/errors exist | Tunnel/path SLA probe worsens while other paths remain healthy |
| Application degradation | Many flows share a saturated bottleneck | Affected interface is unstable | Primary tunnel users affected; backup path is healthy |

The class is a probability and must be revised after operator investigation.

## Severity

Severity combines likelihood, user/service impact, and scope. It is not determined by a single metric.

| Severity | Example condition | Operator expectation |
|---|---|---|
| Informational | Trend changes but risk is below alert policy | Observe or collect more data. |
| Warning | Calibrated near-term risk; no current app impact | Review evidence and readiness of backup path. |
| High | Likely impact soon for one critical path/application | Open incident and consider controlled mitigation. |
| Critical | Current or imminent wide-scope service impact | Escalate; use approved emergency process. |

## Affected scope

Scope is derived by joining entity IDs to topology and app probes:

~~~text
Root candidate: Branch-01 WAN-primary interface
Path: Branch-01 → PE-A → Core → PE-B → DC-01
Sites: Branch-01, DC-01
Applications: video and file upload
Fallback: Branch-01 → DC-01 backup path available
~~~

Do not report every device in the topology as affected. Only show entities with graph/path relation or direct supporting evidence.

## Alert eligibility

An incident card is eligible only when it has:

- an incident-type probability;
- root/affected entities connected to a known topology version;
- data-quality status;
- raw evidence IDs;
- a time-to-impact result if described as predictive; and
- an approved runbook or a clear statement that manual investigation is needed.

