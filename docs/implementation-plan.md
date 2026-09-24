# Implementation plan

## Delivery principle

Build a vertical slice first: one real path, one health signal, one gradual fault, one predictive warning, one approved runbook, and one visible recovery. Do not wait for every component before proving the full loop.

## Phase 0 - repository and success contract

| Deliverable | Definition of done |
|---|---|
| Documentation baseline | This docs set reviewed; MVP scenario and impact thresholds agreed. |
| Repository scaffold | Directories for lab, telemetry, analytics, backend, dashboard, docs, tests, and experiment artifacts. |
| Version conventions | Run ID, topology/configuration/model/dataset/feature versions defined. |
| Demo acceptance test | Timestamped early-warning and recovery criteria agreed before implementation. |

## Phase 1 - real network and traffic

| Deliverable | Definition of done |
|---|---|
| Lab topology | Branch, core, DC, primary path, backup path, and separate management network work. |
| App traffic | Chat, image/file upload, and video/probe flow through primary path. |
| Controlled mitigation | Operator can verify and shift to backup path, then restore normal state. |
| Fault harness | Gradual loss/jitter fault manifest plus reset procedure work repeatedly. |

## Phase 2 - telemetry and data

| Deliverable | Definition of done |
|---|---|
| Inventory/topology | Every monitored entity maps to site, role, interface/tunnel/path, capacity, and version. |
| Collector | At least loss/latency/jitter or interface drops plus app health arrive every 5 seconds locally. |
| Normalization | Metric/event records validate against the documented schema. |
| Storage | Raw metrics, events, manifests, and app probes are retained by run ID. |
| Dataset runs | Normal and randomized fault runs are collected with valid labels. |

## Phase 3 - predictive MVP

| Deliverable | Definition of done |
|---|---|
| Feature job | Creates versioned rolling features and data-quality status. |
| Trend baseline | Detects gradual deterioration in recorded training runs. |
| LightGBM model | Trained/calibrated on complete training/validation runs. |
| Topology correlator | Produces ranked root/path/site scope from live scores. |
| Evaluation | Untouched test-run results and model card exist. |

## Phase 4 - incident workflow and RAG

| Deliverable | Definition of done |
|---|---|
| Backend/API | Serves topology, evidence, predictions, actions, and feedback. |
| Dashboard | Shows topology, incident card, evidence, action state, and closure form. |
| Runbook corpus | At least one approved backup-path playbook with validation/rollback steps. |
| Local retrieval | Returns applicable local documents with citations/metadata. |
| Structured copilot | Optional local LLM passes JSON-schema tests; dashboard fallback works without it. |

## Phase 5 - demo and feedback loop

| Deliverable | Definition of done |
|---|---|
| Held-out scenario | Not used in model/threshold selection; repeatably predicts before defined impact. |
| Real demo recording | Full presenter script runs with saved evidence. |
| Closure/review | Feedback form creates draft then reviewer-approved incident memory. |
| Retraining prototype | A reviewed dataset snapshot can train/evaluate a challenger offline. |
| Security/audit | Local-only assumptions, approvals, and artifact audit trail are demonstrable. |

## Suggested team streams

| Stream | Owns |
|---|---|
| Network/app | Lab topology, routing/paths, demo app, fault harness, recovery. |
| Telemetry/data | Collectors, schema, inventory, storage, labels, experiment artifacts. |
| Analytics | Features, baseline/LightGBM, topology correlation, evaluation. |
| Product/ops | Backend, dashboard, RAG corpus, feedback/action workflow, demo presentation. |

## Stop/go gates

| Gate | Continue only when |
|---|---|
| Move from network to ML | Telemetry and app-health timestamps are reliable. |
| Train a classifier | Sufficient diverse labelled runs exist; a simple baseline has been measured. |
| Add a local LLM | Structured incident card and RAG retrieval already work without it. |
| Add a neural model | It has enough independent data and a clear baseline to beat. |
| Claim prediction in demo | Held-out fault alert precedes the predeclared impact threshold. |

