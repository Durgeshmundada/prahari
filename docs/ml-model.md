# ML model: inputs, outputs, and prediction

## Direct answer

Yes, PRAHARI needs a prediction model to make an honest “before failure” claim. For the first real demo, it should **not** start with a large neural network.

Use this MVP stack:

1. **Trend baseline:** rolling slope, EWMA, robust z-score, and threshold-crossing logic.
2. **Risk/type model:** calibrated LightGBM trained on rolling telemetry features.
3. **Location logic:** topology-aware correlation over known device/interface/tunnel/path IDs.
4. **Time-to-impact:** risk probabilities for fixed time horizons plus a metric threshold-crossing estimate.
5. **Recommendation:** local RAG over approved runbooks and reviewed cases; this is separate from the predictor.

Later, once the lab has enough diverse verified runs, add a small multi-task GRU. A “feedback neural network” must never learn blindly from its own outputs or unreviewed operator text.

## Prediction contract

For every monitored entity/path at time `t`, PRAHARI returns:

~~~json
{
  "prediction_id": "pred-20260813-001",
  "model_version": "risk-lgbm-v1",
  "entity_id": "tunnel:branch-01-to-dc-primary",
  "likely_incident_type": "tunnel_degradation",
  "incident_type_probabilities": {
    "normal": 0.03,
    "congestion": 0.14,
    "physical_or_interface_loss": 0.09,
    "tunnel_degradation": 0.74
  },
  "impact_risk": {
    "within_30_seconds": 0.28,
    "within_60_seconds": 0.63,
    "within_120_seconds": 0.84
  },
  "time_to_impact": {
    "range": "1-2 minutes",
    "basis": "120-second horizon plus projected loss threshold"
  },
  "root_location_candidates": [
    {
      "entity_id": "interface:branch-01-edge:wan-primary",
      "site_id": "site:branch-01",
      "score": 0.82
    }
  ],
  "affected_scope": {
    "paths": ["path:branch-01-to-dc:primary"],
    "applications": ["video", "file-upload"],
    "sites": ["site:branch-01", "site:dc-01"]
  },
  "confidence": 0.78,
  "data_quality": "good",
  "evidence": [
    "packet loss rose from 0.2% to 4.1% in 60 seconds",
    "jitter EWMA is 3.7 standard deviations above baseline",
    "video probe loss is rising on the same primary path"
  ]
}
~~~

`within_30_seconds` must never be greater than `within_60_seconds`, and so on. The UI presents a range, not a fake exact deadline.

## Raw inputs

Collect input per device, interface, tunnel, and path. Use rates/deltas rather than raw counters.

| Input group | Required MVP fields | Later fields |
|---|---|---|
| Interface/link health | RX/TX rate, capacity, utilization, drop rate, error rate, interface state | CRC/input/output error subtypes, queue occupancy. |
| Tunnel/SLA health | Loss, RTT/latency, jitter, path/tunnel state, flap count | IPSec rekey failures, BFD/probe diagnostics. |
| Routing events | BGP/OSPF event count, neighbor state change count | Route withdrawals, convergence duration, path attributes. |
| Device health | CPU, memory if available | Process restarts, hardware sensors. |
| Application health | Chat latency, upload duration/throughput, video packet loss/jitter/bitrate | Per-class SLA breach counts. |
| Context | Entity ID, device role, site, capacity, primary/backup relation, topology version | Change window, maintenance state, configuration drift score. |
| Data quality | Missing sample count, source freshness, counter-reset flag | Collector/source health history. |

The normalized record shape is defined in [telemetry-and-data-contract.md](telemetry-and-data-contract.md).

## Feature vector

At each 5-second step, build a rolling 60-second feature vector per entity/path. The first version should include approximately 40–60 features, not thousands.

| Feature class | Examples |
|---|---|
| Current state | latest loss, latency, jitter, utilization, queue/drop/error rate |
| Summary | mean, maximum, p95, standard deviation |
| Trend | slope, change from previous window, acceleration |
| Baseline deviation | EWMA, residual, robust z-score |
| Event context | BGP flap count, tunnel state changes, recent config change |
| Cross-signal relations | loss-to-utilization ratio, loss/jitter correlation, app-health correlation |
| Topology context | neighboring anomaly score, path role, backup availability |
| Data quality | fraction missing, age of newest sample |

Feature generation must be deterministic and versioned. Training and live prediction must call the same feature code.

## How prediction works

### 1. Find early abnormal trends

The baseline detects a metric deviating from normal before it crosses a service-impact threshold.

Example:

~~~text
Time       Loss       Jitter
10:00      0.2%       4 ms
10:20      0.8%       7 ms
10:40      2.1%       14 ms
11:00      4.1%       26 ms
~~~

An EWMA/z-score and slope can flag the deterioration even while loss remains below the chosen impact threshold.

### 2. Estimate impact risk and incident type

A calibrated LightGBM classifier accepts the feature vector and outputs:

- risk that the defined application/service impact occurs within 30, 60, and 120 seconds;
- probabilities for the supported incident types; and
- feature importance/contribution values for explanation.

Train either one model per horizon or a multi-output wrapper. Calibrate each probability on validation data. The raw classifier score is not automatically a trustworthy percentage.

### 3. Find location and affected scope

The model begins with an entity that already has an inventory and topology identity. The correlator ranks likely roots by:

1. earliest and strongest local precursor score;
2. health deterioration on the specific interface/tunnel/path;
3. correlated symptoms on adjacent/downstream entities;
4. the known primary/backup and route relationship; and
5. application probes sharing that path.

It returns a ranked list, not an invented single root cause:

~~~text
Likely root: Branch-01 WAN-primary interface (0.82)
Related path: Branch-01 → DC-01 primary tunnel
Affected site: Branch-01 / Pune
Alternative: Provider-edge-A interface (0.41)
~~~

### 4. Estimate time-to-impact

Define an impact threshold before training. Examples:

- video: loss ≥ 5% or jitter ≥ 30 ms for a sustained interval;
- file upload: transfer time exceeds the test SLA; and
- network path: loss ≥ 8% for a sustained interval.

The model predicts the probability of crossing an impact threshold inside each horizon. Trend logic independently projects when the key metric may cross its threshold. The UI reports an interval only when both pieces of evidence agree enough:

~~~text
Risk: 84% within 120 seconds
Projected loss threshold: 75–110 seconds
Display: “likely service impact in 1–2 minutes”
~~~

If they disagree, show the horizon probability and lower confidence rather than fabricating an ETA.

## Training labels

Create labels from the actual observed impact, not only from the fault-injection script.

| Label | Meaning |
|---|---|
| `incident_type` | Congestion, interface/physical loss, tunnel degradation, routing instability, policy drift, or normal. |
| `root_entity_id` | Verified device/interface/tunnel responsible for the fault. |
| `affected_entity_ids` | Links, paths, sites, and applications affected. |
| `actual_impact_start_time` | When the defined user/application impact began. |
| `time_to_impact_seconds` | Actual impact time minus window end. |
| `impact_within_30/60/120_seconds` | Horizon labels derived from actual impact time. |
| `operator_verified_root_cause` | Reviewed diagnosis; absent until verified. |
| `resolution_outcome` | Resolved, partially resolved, failed, false positive, or uncertain. |

Only call a sample predictive if the application/service impact had not started when the prediction window ended.

## Model training procedure

1. Run normal traffic and randomized fault scenarios in the lab.
2. Store raw telemetry, application measurements, topology/configuration versions, fault manifest, and closure review.
3. Generate features and labels.
4. Split complete runs into train, validation, and test sets—never split overlapping windows from the same run across sets.
5. Train baseline rules and LightGBM with class weighting for rare faults.
6. Calibrate probability outputs using the validation set.
7. Select alert thresholds based on false-alert rate and useful lead time.
8. Evaluate once on the untouched test set.
9. Register the model, features, dataset version, metrics, and approval decision.

See [data-and-labelling.md](data-and-labelling.md) for data generation and split rules.

## Why not start with a neural network?

Neural time-series models can help later, but a small lab dataset is usually too limited and repetitive. A complex model may memorize a fault script instead of learning a transferable precursor. A trend baseline plus LightGBM:

- trains with fewer labelled runs;
- exposes feature importance;
- is fast to iterate for a live demo; and
- gives a baseline that any future neural model must beat.

## Future neural model: gated upgrade

Add a neural model only after collecting hundreds of independent, randomized fault runs per important class and proving a benefit on untouched test runs.

Suggested architecture:

~~~text
12 time steps × 40–60 features
          │
          ▼
     GRU encoder
          │
          ▼
 shared dense representation
   ├── risk-by-horizon head
   ├── incident-type head
   └── time-to-impact interval head
~~~

Train it only from reviewer-verified labels. It must be versioned, evaluated against the existing baseline, and reversible. It does not learn directly from the LLM’s wording, generated recommendation, or its own predictions.

## Model limitations to state honestly

- Predictions are probabilistic and depend on telemetry quality.
- A model trained in one lab topology is not automatically valid for every vendor or network.
- Similar symptoms can have different causes; location is ranked evidence, not certainty.
- A predicted risk does not authorize a router change.
- No quality claim is valid until it is measured on held-out runs.

