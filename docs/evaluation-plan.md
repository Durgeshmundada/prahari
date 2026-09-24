# Evaluation plan

## Purpose

PRAHARI may claim predictive value only after measuring it on runs that were not used to train, tune, calibrate, or select the model.

## Primary measures

| Measure | Meaning |
|---|---|
| Incident precision | Of alerted incidents, how many were real upcoming/actual incidents? |
| Incident recall | Of real incidents, how many were alerted? |
| False alerts per link-hour | How noisy is the system during normal operation? |
| Useful lead time | How long before defined application impact did a valid alert arrive? |
| Incident-type macro F1 | How well does it distinguish supported classes? |
| Root-location top-1/top-3 accuracy | Whether the ranked location includes the verified root. |
| ETA interval coverage/error | Whether actual impact lands in/near the stated time range. |
| Probability calibration | Whether an 80% predicted risk behaves like roughly 80% over many cases. |
| Case retrieval relevance | Whether the approved returned cases/runbooks actually apply. |
| Recommendation outcome | Accepted/resolved/failed outcomes, measured separately from prediction quality. |

## Definition of a successful prediction

For an incident run, count a successful early warning only if all conditions hold:

1. the alert appears before the defined application/service impact starts;
2. the correct affected scope/root appears in the top-ranked results according to the declared metric;
3. the model data-quality state is acceptable;
4. the alert belongs to a supported incident class; and
5. the evidence and timestamps are retained.

An alert after the video/file/chat impact starts is detection, not prediction.

## Evaluation procedure

1. Freeze train, validation, test, and demo-holdout run lists by run ID.
2. Train/tune only on train and validation runs.
3. Freeze feature definitions, model parameters, calibration, and alert policy.
4. Evaluate on the untouched test runs.
5. Report normal and fault-run performance separately.
6. Conduct the live demo with the separate holdout.
7. Archive all artifacts for reproducibility.

## Owned-pilot MVP result (2026-09-20)

The first measured candidate used whole-run partitions from 15 impacted and 12
normal runs. The untouched test partition contained two impacted and two normal
runs. The 120-second technical gate failed, so this candidate is not approved or
activated.

| Metric | Result | Test run count | Notes |
|---|---:|---:|---|
| Incident precision | 0.667 | 4 | Required at least 0.70 at the 120-second policy. |
| Incident recall | 1.000 | 4 | Required at least 0.60. |
| Median useful lead time | 100.15 seconds | 2 impacted | Measured from retained application-impact timestamps. |
| False alerts | 38.81 / eligible negative link-hour | 0.0515 eligible hours | Required at most 1.0; two false-alert episodes. |
| Root top-1 accuracy | 1.000 | 2 impacted | Fixed-scope topology-manifest mapping, not an ML localization head. |
| ETA interval coverage | 0.500 | 2 impacted | One of two eligible incidents covered. |

The demo holdout was excluded from the gate and also remained untouched during
selection: its 120-second precision/recall were 0.500/1.000, false-alert rate was
96.91 per eligible negative link-hour, and ETA coverage was 0.500. The small
denominators and short negative exposure make these estimates uncertain, but do
not justify waiving the failed thresholds. Define a new experiment/model version
before changing features, thresholds or policy, and evaluate it on fresh test and
demo-holdout runs. Exact hashes and metrics are in
`artifacts/owned-model-evaluation`.

The next challenger, `owned-risk-lgbm-horizons-v3`, completed its separate
18-runs-per-outcome campaign. Deterministic whole-run splitting retained 24 runs
for training and four each for validation, test and demo holdout. Validation met
the 120-second selection constraints with precision/recall 1.0/1.0, zero
false-alert episodes and 97.40 seconds median useful lead. The untouched test
split retained 1.0/1.0 precision/recall, 102.08 seconds median lead and a 0.0728
Brier score, but one false-alert episode over 0.0434 eligible negative link-hours
yielded 23.05 alerts/hour against the 1.0 maximum. The technical gate failed. The
excluded demo holdout reported 1.0/1.0 precision/recall, zero false-alert episodes
and 111.35 seconds median lead. Exporter, trainer, cohort and outputs are
checksum-inventoried. V3 is unapproved, unscoped and inactive; its revealed test
and demo results cannot be used to tune that candidate.

The next experiment uses `technical-evaluation-v2`. Alongside the observed
false-alert rate, validation and test must each pass an exact one-sided 95%
Poisson upper bound of at most 1.0 alert per eligible negative link-hour. Even
with zero observed episodes, this needs about three eligible negative link-hours
in each gated split. The next collection plan therefore needs long independent
normal soaks; repeating two short clean runs cannot establish the reliability
claim. The v4 collection contract uses 18 runs per class and a roughly 50-minute
normal-run baseline, then assigns whole normal runs until validation, test and
demo holdout each have at least three eligible negative link-hours. It retains at
least six normal training runs and balances traffic profiles. This collection is
prospective; v3 test/demo results are not inputs to v4 threshold selection.

## Model-promotion gate

Promote a retrained challenger only if it:

- improves or maintains critical precision and false-alert limits;
- improves a pre-agreed target without reducing useful lead time below the acceptable value;
- remains calibrated;
- does not regress on important incident classes;
- passes safety/review requirements; and
- can be rolled back.

## Claims to avoid

Do not claim:

- “predicts every network failure”;
- “works for any network without adaptation”;
- “100% accurate”;
- “autonomously resolves incidents”; or
- “air-gapped” without showing the actual deployment/egress boundary.

Use measured, scoped claims instead:

> In our held-out lab scenarios, the MVP predicted the defined gradual primary-path degradation before the application-impact threshold with the reported lead time and false-alert rate.
