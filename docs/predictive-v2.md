# Predictive-v2 evidence and model workflow

PRAHARI's `degradation-lgbm-v1` artifact is an experimental current-state
detector. It is deliberately not presented as forecasting evidence. The
predictive-v2 path below is the separate, fail-closed route to a genuine
future-impact claim.

## Evidence chain

Each five-second experiment sample contains two measurements from the same
Branch-01-to-DC path:

1. an ICMP path measurement for loss, RTT, and jitter; and
2. the real WebSocket chat plus bounded file-transfer measurement for
   application message loss, RTT, jitter, completion, integrity, and throughput.

The raw observation also records the applied `tc netem` condition, but that
condition is never used as the outcome label. `actual_impact_start_time` is the
first observation in a sustained application breach that is later confirmed
for 15 seconds. The versioned policy is
[`config/predictive-v2.json`](../config/predictive-v2.json).

A completed fault run retains a planned-before-apply and applied acknowledgement
for every fault step. Finalization verifies that file, the exact observation
sequence, API persistence, cadence, impact calculation, and SHA-256 checksum
before sealing `manifest.json`.

## Collect real runs

Run these commands inside Ubuntu/WSL from the repository root. The base data
contract requires at least 12 independent runs with observed impact and 12
without impact. The short batch rotates three fault profiles and three traffic
profiles without allowing one failed child run to abort the campaign, but its
short normal runs cannot satisfy the uncertainty-aware promotion gate.

```bash
bash network-lab/scripts/run-predictive-batch.sh \
  --runs-per-class 12 \
  --confirm RUN-PREDICTIVE-BATCH
```

One normal run takes roughly 3.5 minutes and one gradual-loss run roughly five
minutes at the frozen five-second cadence. Each attempt has a separate retained
log. Interrupted or failed runs remain unsealed and cannot enter a dataset; the
batch retries within a fixed bound and writes a completed or incomplete summary.
The cleanup trap removes only the controlled netem qdisc.

For `technical-evaluation-v2`, use the root-protected owned campaign with at
least 18 runs per class and a 600-sample normal baseline. Each normal run lasts
about 50 minutes, with a deterministic small duration variation. The exporter
allocates whole normal runs until validation, test and demo holdout each contain
at least three eligible negative link-hours, while retaining at least six normal
training runs and all traffic profiles. The campaign summary binds the sample
baseline, so a resumed collection cannot change it.

```bash
python scripts/run_owned_model_campaign.py \
  --state-dir /protected/prahari-owned-pilot \
  --runs-per-class 18 \
  --normal-samples 600 \
  --base-seed <predeclared-seed>
```

Individual runs are also available:

```bash
bash network-lab/scripts/run-predictive-experiment.sh \
  --scenario normal --confirm RUN-PREDICTIVE-V2

bash network-lab/scripts/run-predictive-experiment.sh \
  --scenario gradual_loss --profile b --confirm RUN-PREDICTIVE-V2
```

## Freeze a dataset

The exporter replays every impact label, validates every evidence checksum, and
assigns whole runs using deterministic impact-stratified splits. Validation,
test, and demo-holdout each receive two impacted and two no-impact runs. No
overlapping window from a held-out run can enter training.

```bash
uv run python scripts/export_predictive_dataset.py \
  --runs-dir network-lab/state/predictive-experiments \
  --policy config/predictive-v2.json \
  --output artifacts/training/predictive-track-a-v2.jsonl
```

The exporter refuses to overwrite an existing dataset. A new set of runs must
produce a new versioned output name. Negative labels whose complete future
horizon was not observed remain censored instead of being guessed.

## Train and evaluate

```bash
uv run python scripts/train_predictive_model.py \
  --dataset artifacts/training/predictive-track-a-v2.jsonl \
  --output-dir artifacts/models/risk-lgbm-horizons-v4
```

The trainer builds one LightGBM classifier for each 30-, 60-, and 120-second
horizon, fits a deterministic Platt calibration map only on validation runs,
and chooses thresholds only on validation data. Under technical-evaluation-v2, the primary
120-second threshold must satisfy the declared incident precision, recall,
observed false-alert, one-sided 95% false-alert upper-bound, and useful-lead
constraints on validation data; otherwise training
writes an atomic structured rejection, inventories every assessed threshold, marks
test and demo holdout unevaluated, and stops before either holdout is read. The
shorter horizon thresholds remain validation row-F1 selections for ETA banding. A
candidate that
passes validation evaluates test and immutable demo-holdout runs separately and
records:

- row precision, recall, F1, false-positive rate, Brier score, and calibration error;
- incident precision and recall;
- false alert episodes per eligible negative-exposure link-hour, plus the exact
  one-sided 95% Poisson rate upper bound, both with a predeclared test gate of at
  most 1.0 per hour. With zero episodes, about three eligible negative link-hours
  are required before the confidence-bound gate can pass;
- useful lead-time distribution;
- ETA interval coverage; and
- fixed-scope topology/manifest localization, explicitly distinguished from an
  ML localization head.

The generated manifest and model card remain `evaluation_pending` even if the
technical gate passes. Training never self-approves deployment.
Approval and runtime activation require the complete `technical-evaluation-v2`
policy and every named validation/test check. A legacy or hand-edited gate that
only contains `"passed": true` fails closed.

## Accountable activation

An independent reviewer creates a `predictive-model-approval-v2` JSON record
bound to the exact model version and dataset SHA-256. Controlled-lab approval
must acknowledge the technical gate, untouched holdout, and limitations.
Production approval additionally requires an external validation evidence hash
and change ticket.

```bash
uv run python scripts/approve_predictive_model.py \
  --manifest artifacts/models/risk-lgbm-horizons-v4/manifest.json \
  --approval-record /protected/review/approval-record.json \
  --confirm APPROVE-PREDICTIVE-V2
```

Only an approved, checksum-valid bundle is allowed to replace heuristic risk at
runtime. Configure its manifest with
`PRAHARI_PREDICTIVE_MODEL_MANIFEST_PATH`. An unapproved candidate may be loaded
for model inventory and offline evaluation, but `score()` rejects runtime use.

## Scientific boundary

Passing this lab gate supports only the versioned Containerlab path, ramp family,
and application-impact policy. It does not prove performance on an unseen vendor,
topology, traffic distribution, or production network. A prediction never
authorizes an action; the existing review, dry-run, explicit approval, validation,
and rollback workflow still applies.
