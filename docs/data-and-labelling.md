# Data generation, dataset, and labelling

## Primary dataset strategy

Create the primary dataset in the same Containerlab/FRRouting SD-WAN/MPLS-first lab used for the real demo. This gives ground truth for:

- which fault was injected;
- where it was injected;
- when it began and ended;
- which user/application symptoms appeared;
- which action resolved it; and
- whether the prediction arrived before impact.

Public datasets can help create realistic background traffic patterns or test preprocessing. They are not proof that the PRAHARI lab model predicts SD-WAN/MPLS incidents. Generic intrusion-detection datasets are especially unsuitable as fault-prediction labels.

## Dataset composition

| Category | What to record |
|---|---|
| Normal runs | Different load profiles, durations, times, path selections, and no injected fault. |
| Gradual loss/jitter | Location, loss slope, maximum loss, duration, traffic load, actual app impact. |
| Congestion | Offered load, bottleneck, QoS class, queue/drop behavior, app impact. |
| Routing instability | Event timing, affected prefixes/path, convergence, downstream symptoms. |
| Tunnel degradation | Underlay/overlay state, tunnel events, loss/jitter/rekey behavior. |
| Policy/configuration drift | Configuration diff, scope, symptom, rollback/repair result. |
| Recovery | Action taken, timestamp, before/after network and app health. |

The MVP only needs normal runs plus the first gradual loss/jitter scenario. Generate the others only when the related feature/adapters exist.

## Fault manifest

Every scenario writes a manifest before injection. This prevents mystery labels.

~~~json
{
  "run_id": "run-2026-08-13-042",
  "scenario": "gradual_packet_loss",
  "random_seed": 8241,
  "topology_version": "lab-v1",
  "config_version": "lab-config-17",
  "fault_id": "fault-01",
  "target_entity_id": "interface:branch-01-edge:wan-primary",
  "start_time": "2026-08-13T10:00:00Z",
  "end_time": "2026-08-13T10:04:00Z",
  "schedule": [
    {"offset_seconds": 0, "loss_percent": 0.2},
    {"offset_seconds": 60, "loss_percent": 1.0},
    {"offset_seconds": 120, "loss_percent": 3.0},
    {"offset_seconds": 180, "loss_percent": 6.0}
  ],
  "expected_incident_type": "tunnel_degradation"
}
~~~

Record the actual observed impact separately; a fault schedule is not itself proof of user impact.

## What counts as ground truth

| Question | Ground truth source |
|---|---|
| Was a fault injected? | Immutable fault manifest and lab controller log. |
| Where did it begin? | Target entity in manifest, checked by telemetry and operator review. |
| When did user impact start? | Timestamped application-health probe/SLA measurement. |
| What kind of incident was it? | Manifest plus reviewed evidence and closure record. |
| Did the action work? | Before/after validation metrics and operator outcome. |

## Feature-window label example

~~~text
run_id: run-042
window_end: 10:01:00
actual_impact_start: 10:02:25
root_entity: Branch-01 WAN-primary
incident_type: tunnel_degradation
impact_within_30_seconds: false
impact_within_60_seconds: false
impact_within_120_seconds: true
time_to_impact_seconds: 85
~~~

Windows recorded after impact starts may be useful for diagnosis, but must not be counted as successful early-warning samples.

## Variation prevents memorization

Randomize independently:

- affected site, interface, tunnel, and path;
- fault start time, duration, slope, and severity;
- background traffic volume and mix;
- application mix;
- source/destination paths;
- normal-run conditions; and
- recovery action timing.

Do not always use the same branch, exact 1% → 3% → 6% loss ramp, and traffic level. Otherwise the model can merely recognize the script.

## Train, validation, test, and demo split

Split by complete `run_id`, not by individual time windows.

| Split | Purpose | Rule |
|---|---|---|
| Training | Fit model parameters | May contain many randomized normal/fault runs. |
| Validation | Tune hyperparameters, calibration, and alert threshold | Must use different runs from training. |
| Test | Final model-quality estimate | Never use to choose features or thresholds. |
| Demo holdout | Live or recorded presentation scenario | Never use for training, tuning, threshold selection, or model choice. |

Example allocation:

~~~text
70% complete runs → training
15% complete runs → validation
15% complete runs → test
1 additional complete run → live-demo holdout
~~~

The demo holdout must be generated with a previously recorded seed but unseen by the modeling process. It can be replayed reliably without being part of training.

## Dataset record fields

| Group | Required fields |
|---|---|
| Identity | `run_id`, record time, entity ID, topology version, config version |
| Raw evidence | Metric/event records, application probe values |
| Scenario | Fault manifest ID, random seed, traffic profile ID |
| Labels | Incident type, root, affected scope, actual impact time, horizon labels |
| Review | Operator diagnosis, reviewer decision, outcome, playbook ID |
| Provenance | Collector version, feature version, model version where applicable |

## External data

Use external data only as supplementary material:

| Need | Safe use of external data | Do not do |
|---|---|---|
| Background load | Replay/anonymize legitimate traffic-shape traces where permitted. | Claim it represents the team’s SD-WAN/MPLS fault conditions. |
| Code testing | Test parsers and feature pipelines against public formats. | Mix unrelated labels into the ground-truth fault dataset. |
| Research comparison | Cite official papers/datasets in the final report. | Train a fault predictor on generic cyberattack labels and call it network-degradation prediction. |

## Dataset quality gate

A run is eligible for training only if:

- telemetry and application probe timestamps are aligned;
- its topology/configuration version is known;
- the fault manifest is present, or the normal-run status is verified;
- the affected entity has a valid inventory mapping;
- data-quality checks pass; and
- labels are reviewed for any non-synthetic or ambiguous incident.

