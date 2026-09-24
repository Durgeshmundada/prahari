# Operator feedback, incident memory, and safe learning

Implementation status: the API and dashboard persist closure submissions with
incident/feature-window snapshots and independent final review decisions.
Compatible approved cases now enter bounded copilot context. The richer
normalized feedback fields, similarity search, and training-label pipeline
described below remain design targets. See the
[implemented API contract](api-and-ui-contract.md) for the current case schema.

## The right feedback design

Network caretaker feedback is valuable, but it must not create an uncontrolled self-training network. PRAHARI has two separate learning paths:

~~~mermaid
flowchart TB
    P[Telemetry predictor] --> I[Structured incident prediction]
    I --> R[Local RAG and approved case retrieval]
    R --> O[Operator investigates and resolves]
    O --> F[Reviewed closure form]
    F --> C[Reviewed incident memory]
    F --> L[Verified labels in versioned dataset]
    C --> R
    L --> T[Scheduled offline model training]
    T --> E[Held-out evaluation and approval]
    E --> P
~~~

| Path | Learns | When it changes |
|---|---|---|
| Incident memory/RAG | What a reviewed operator did, why it worked, and where it applies | After reviewer approval of a case. |
| Prediction model | Which telemetry patterns precede verified impact | Only in a controlled offline retraining cycle. |

The ML model predicts **what may fail**. The reviewed incident memory helps retrieve **what may be safe to try**. Neither path authorizes autonomous changes.

## Closure form

At the end of every investigated alert, the caretaker records structured feedback:

| Field | Allowed values / example |
|---|---|
| Alert verdict | Correct, false positive, uncertain |
| Actual incident type | Congestion, interface loss, tunnel degradation, routing instability, policy drift, other |
| Verified root entity | `interface:branch-01-edge:wan-primary` |
| Affected scope | Sites, paths, applications, users |
| Actual impact start | Timestamp or `no impact` |
| ETA usefulness | Early enough, too late, false/uncertain |
| Observed error | “Loss rose after primary path impairment; video jitter breached SLA.” |
| Action taken | Approved playbook ID and steps actually performed |
| Steps that worked | “Validated backup path, shifted traffic, confirmed video recovery.” |
| Steps that failed | “Initial tunnel reset did not reduce loss.” |
| Before/after evidence | Metric and app-probe references |
| Outcome | Resolved, partial, failed, escalated |
| Review status | Draft, reviewer approved, rejected |

Free text is useful, but it is not a training label until a reviewer normalizes and approves it.

## Incident-memory case

Each approved case is saved locally in a structured form plus a reviewed narrative:

~~~json
{
  "case_id": "case-2026-008",
  "review_status": "approved",
  "topology_version": "lab-v1",
  "applicability": {
    "device_roles": ["branch-edge", "dc-edge"],
    "incident_type": "tunnel_degradation",
    "requires_backup_path": true
  },
  "symptoms": [
    "rising loss",
    "jitter above baseline",
    "video probe degradation"
  ],
  "verified_root_entity_id": "interface:branch-01-edge:wan-primary",
  "approved_playbook_id": "runbook-backup-path-shift-v1",
  "steps_that_worked": [
    "Verify backup-path health",
    "Request operator-approved traffic shift",
    "Validate application recovery"
  ],
  "steps_that_failed": [
    "Do not rely on a tunnel reset alone when underlay loss persists"
  ],
  "outcome": "resolved",
  "evidence_ids": ["run-042", "prediction-101", "validation-32"]
}
~~~

When a new incident occurs, PRAHARI retrieves cases only after matching incident type, symptoms, topology/configuration context, and safety prerequisites. Use a `case_match_score`, not the incorrect phrase “85% recall confidence.” Similarity is not proof.

## How feedback improves future predictions

Reviewed feedback supplies better labels:

- a correct prediction strengthens the association between precursor and verified impact;
- a false positive helps tune thresholds and supplies a negative example;
- a corrected incident type/root updates labels;
- actual impact time improves time-to-impact targets;
- a failed action updates playbook review, not merely model weight; and
- topology/configuration changes define applicability boundaries.

## Offline retraining cycle

Do not retrain during an incident. Suggested controlled loop:

1. Accumulate a defined batch of newly reviewed cases/runs, for example 50 new eligible cases or a scheduled review.
2. Validate records against the data-quality and label rules.
3. Freeze a versioned dataset snapshot.
4. Train a challenger model offline.
5. Compare it with the current model on untouched test runs.
6. Check prediction accuracy, calibration, useful lead time, and false-alert rate.
7. Require reviewer/owner approval before promotion.
8. Register the model and retain rollback to the earlier version.

## Guardrails

- No online self-training.
- No use of an LLM answer as a ground-truth label.
- No use of unreviewed free-text operator notes as a training target.
- No recommendation becomes executable merely because a similar case exists.
- Old cases are downgraded or excluded if topology, software, policy, or configuration context changed.
- Keep evidence, feedback author, reviewer, data version, and model version in the audit trail.
- Failed or rejected actions enter a playbook-review queue.

## What to call it in the presentation

Accurate statement:

> “The system learns in two governed ways: approved past resolutions become retrievable local incident memory immediately, and verified outcomes are added to periodic offline supervised training after evaluation.”

Avoid saying:

> “The AI automatically learns and applies whatever solution an operator writes.”
