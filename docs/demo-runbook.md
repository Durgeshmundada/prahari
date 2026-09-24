# Real-demo runbook

## Demo claim

This demo proves that PRAHARI predicts and helps prevent a degradation on a real local application path. It must not be presented as a production deployment or as a model that works on every network without adaptation.

## One-sentence narrative

> A branch user sends chat, an image, and video traffic to a data-centre service across a primary SD-WAN/MPLS path. PRAHARI detects a gradual, held-out degradation, predicts likely impact at the Branch-01 primary path in 1-2 minutes, retrieves an approved backup-path playbook, and shows recovery after operator approval.

## Prerequisites

| Item | Verification |
|---|---|
| Lab topology running | Primary and backup data paths are reachable. |
| Management plane running | Dashboard/collector remain available independently of primary path. |
| Local demo application | Chat, upload, and video/probe endpoints work through the primary path. |
| Telemetry | Loss/latency/jitter or equivalent key metrics arrive at the expected cadence. |
| Predictor | A registered model is loaded and has passed a held-out evaluation. |
| RAG corpus | Approved primary-degradation/backup-path runbook and one reviewed case exist. |
| Action control | Dry-run/validation method is tested; rollback/fallback is known. |
| Demo fault | Fault manifest is recorded and excluded from training/tuning. |
| Reset method | Primary path can be restored quickly and deterministically. |

## Evidence to prepare before the audience arrives

1. A screenshot/export showing model evaluation on a test set, separate from the demo holdout.
2. A visible model version, dataset version, topology version, and active runbook version.
3. A healthy baseline: application latency/throughput/video probe plus network health.
4. A saved fault manifest for the held-out gradual-degradation scenario.
5. A clear explanation that the primary data path and management network are separate.
6. A ready rollback/reset procedure.

## Presenter script

### 1. Show the real traffic path - about 45 seconds

- Open the topology view.
- Show Branch-01 client to primary path to DC application server.
- Send a chat message.
- Upload an image/file.
- Start or show a local video stream/probe.
- Point to normal app health and normal network metrics.

Say:

> These applications are really using the primary lab path. PRAHARI is not carrying the traffic; it is observing the routers, tunnel/path probes, and the application health.

### 2. Show local observability - about 30 seconds

- Open the evidence panel.
- Show live loss, latency, jitter, interface/tunnel state, and application probe metrics.
- Show that the collector/dashboard are on the management plane.

Say:

> The router health data stays local. We use entity IDs and the topology map to connect a metric to its router, interface, tunnel, path, and physical site.

### 3. Start the held-out gradual fault - about 15 seconds

- Trigger the explicit pre-recorded fault manifest on the primary path.
- Display the run ID/fault manifest on screen.
- Do not say the predicted answer before the system produces it.

Say:

> This scenario was kept out of training and threshold tuning. The fault increases gradually, so we can test whether the warning arrives before user impact.

### 4. Show early prediction - about 60 seconds

- Show loss/jitter/trend rise before the defined video/file impact threshold.
- Open the new incident card.
- Show likely type, Branch-01 primary path/location, 30/60/120 second risk, time range, confidence/data quality, and specific evidence.

Say:

> The numeric model detects the precursor; the topology correlator maps it to Branch-01 primary path. The time is a probability-backed range, not a claim of an exact failure second.

### 5. Show explanation and safe recommendation - about 45 seconds

- Open the copilot/evidence panel.
- Show citations to the local metric window, approved runbook, and any reviewed similar case.
- Show the required prerequisite: healthy backup path.

Say:

> The local copilot does not predict or execute changes. It explains the model output and retrieves approved local procedures. A similar case is only evidence, not automatic permission.

### 6. Demonstrate human-approved mitigation - about 60 seconds

- Run the playbook dry-run/health checks.
- Have an operator click/record approval.
- Perform a controlled switch/traffic shift to the backup path.
- Show application metrics and video/file behavior recover.

Say:

> The operator remains in control. We validate the backup path first, record approval, make the controlled change, and validate that the user traffic recovered.

### 7. Close the loop - about 30 seconds

- Submit a short closure form: verified root, action, what worked, and outcome.
- Show its initial draft/review status.

Say:

> After review, this becomes a local incident-memory case that can be retrieved next time. Its verified labels can enter a later offline training cycle; the system does not self-train during the incident.

## Success criteria

The demo passes only when:

| Criterion | Proof |
|---|---|
| Real traffic crosses lab path | App/server and topology evidence. |
| Telemetry is local and correctly mapped | Live metric/entity/site/path display. |
| Warning precedes defined app impact | Timestamped prediction and application-health comparison. |
| Location/type/time are traceable | Incident card with evidence IDs and topology context. |
| Recommendation is grounded | Cited approved runbook/case. |
| Human approval gates change | Action/audit record. |
| Recovery is validated | Before/after app and network metrics. |
| Learning loop is governed | Draft/review status and model-training explanation. |

## Fallbacks

| Failure | Fallback |
|---|---|
| Live lab fault is unstable | Replay a timestamped held-out recording and clearly label it as recorded evidence. |
| Local LLM fails | Use structured prediction card plus retrieved runbook links; do not fake a copilot response. |
| Backup failover cannot be automated safely | Demonstrate dry-run and have an operator perform the documented manual switch. |
| Video is resource-heavy | Use a reproducible UDP/RTP probe with visible loss/jitter/bitrate plus chat/upload. |
| Model does not alert early | Do not claim prediction; show the feature/training gap and improve the dataset/thresholds. |

## Reset

After the demo:

1. restore the explicit fault target to healthy state;
2. verify the primary path and application traffic;
3. confirm backup path policy has the expected normal state;
4. stop the demo workload or archive the run with its manifest;
5. save telemetry, prediction, action, and closure evidence under the run ID; and
6. mark the case as draft until reviewer approval.
