# Production build plan and review

Review date: 2026-09-21. This replaces the original demo-oriented sequencing for
the production build. The existing single-path lab remains a development fixture.

## Current review

Implemented: validated ingestion and freshness checks; persistent evidence;
browser sessions and API roles; prediction-bound reviews/dry-runs; independent
closure cases; approved historical context; migrations, grants, backup scripts,
deployment checks, and dashboard component tests.

Production review findings and current disposition:

- `services/demo.py`: external profiles now supply service identities and
  topology/inventory/playbook references. Scope remains one selected path.
- `actions.py` and `network-lab/scripts/action-agent.py`: the adapter contract,
  router interfaces, and neighbour addresses are bound to the controlled lab.
  Removing these restrictions without a verified vendor adapter would widen
  unsafe device access. Production must reject this adapter.
- `analytics/horizon.py`: evidence joins now use configured identities and
  profile/model bindings. Model validation remains tied to a specific experiment
  family; changing identities cannot establish model generalisation.
- `copilot/service.py`: narrative is rendered from verified server statements;
  the model selects IDs only. Typed historical outcomes cannot establish a
  present diagnosis. Free-text case notes are excluded from model context.
- `persistence.py`: incident claims, current-review authorization, action intent
  and audit now commit atomically. Uncertain results survive restart and require
  independent reconciliation. Telemetry can still change around device dispatch;
  verified vendor fencing and target-device acceptance remain open.

The existing experimental model classifies present degradation. No approved
production forecasting bundle or customer-environment acceptance record was
found. A replay walkthrough exists; it is not live recovery evidence.

## Ordered implementation

| Order | Deliverable | Acceptance evidence |
|---|---|---|
| 1 | Validated external network profile; explicit production configuration; lab adapter rejected in production | Alternate site/path identities work through ingestion, incident and workflow APIs; invalid references fail startup |
| 2 | Model applicability bound to deployment scope and evidence schema | Incompatible models fail closed; target dataset, calibration and held-out results exist |
| 3 | Atomic workflow authorisation and durable action reconciliation | PostgreSQL race/failure tests, duplicate dispatch blocked, uncertain outcomes visible |
| 4 | Claim support verification and approved knowledge provenance | Contradictory or unsupported claims rejected; degraded local-model behaviour tested |
| 5 | Production observability and operating procedures | Retention, capacity, HA/recovery, secret rotation, restore and alert drills |
| 6 | Real infrastructure acceptance and security review | Approved vendor adapters, staging load/failure tests, device rollback, network-isolation evidence, scans/SBOM and independent review |

Each change keeps replay usable, documents compatibility, and runs relevant
tests before the next increment. Monitoring configuration is generalised first;
device mutation is enabled only for a verified, explicitly approved adapter.
Protocol constants and validated enums remain part of the safety contract.

## Defence-sector claim boundary

PRAHARI can be developed for customer-controlled, offline network monitoring and
human-controlled operations. The repository does not establish defence-sector
accreditation, suitability for classified data, customer-approved cryptography,
or compliance with a particular procurement standard. Those claims require the
actual customer requirements, threat model, independently reviewed evidence,
operating procedures, and accountable acceptance. Describe current capabilities
and evaluation status instead of asserting certification.

## Own-server pilot first

The user selected an owned server environment before seeking customer acceptance.
Build and measure the isolated [owned pilot](owned-pilot.md), collect independent
normal/gradual-loss runs, and evaluate a candidate forecasting bundle. Keep live
monitoring clearly experimental until calibration and approval are complete.
Customer router vendors, identity provider and accreditation requirements can
follow; no customer or defence approval is manufactured for this pilot.

## Implementation progress

Order 1 implemented: `network_profile.py` validates external reference data;
live service identities, ingestion, incident APIs and case workflows use it.
Production requires explicit scope, an activated model, and read-only profile
and knowledge mounts. The controlled-lab action adapter is refused. Replay keeps
its explicit development fixture. Scope remains one selected monitored path;
multiple independent incident streams and vendor collectors remain open.

Order 2 enforcement implemented: predictive approval records bind the canonical
profile checksum; model scope and window policy must match runtime configuration.
Predictions and immutable cases retain profile/model-bundle checksums. Their
prediction IDs and historical applicability checks include these bindings, so
old reviews cannot authorize changed deployment evidence.
Actual target-dataset collection, training and independent approval remain open.
The checked-in profile is a controlled-lab example, not deployment approval.

Order 4 implemented for the supported statement catalog: external runbooks require exact paths, checksums,
reviewer identities and approval references. Unlisted or changed documents are
excluded/rejected. Evidence selection now verifies statement IDs, source hashes,
support paths, confidence and the complete captured incident. Unsupported prose
is rejected. Independent review of procedural content remains an acceptance gate.

Order 3 implemented for the supported workflow: migration `0004_action_operations`
adds durable operation records and incident ownership. Current-review checks,
ownership, intent and audit commit together; review replacement and closure
cannot bypass an active claim. A unique prediction/kind registration prevents
duplicate dispatch after restart. Pending/unknown records retain their evidence
snapshot and original adapter receipt, remain visible during telemetry failures,
and require an independent reviewer to release the claim in an audited transaction.
The API requires its adapter disabled; stopping other dispatchers is an explicit
reviewer attestation. Automated downstream fencing is still a vendor-adapter gate.
Migration refuses uncorrelated legacy device intents pending an approved import
and reconciliation procedure; it never classifies them as completed automatically.
Migration `0005_case_search` adds and backfills indexed closure-case applicability
metadata for bounded API/dashboard search without changing retained case payloads.

Latest validation: 296 backend tests passed. Six PostgreSQL integration tests
passed against an isolated hardened PostgreSQL 17 container at schema `0005`,
including the repeatable-read whole-store relationship verifier. The `0004` to
`0005` SQLite backfill/index migration passed.
Two Windows symlink-privilege tests skipped. Ruff, all 27 dashboard tests,
TypeScript checks and production build passed. Both Compose templates validated
under WSL; all 32 production security mutations were rejected. Prometheus rules,
rule simulations and configuration syntax passed with verified promtool 3.14.
Real-browser checks covered desktop/mobile operation records, independent-review
controls, and visibility during an actual incident API 503. Evidence is in
`artifacts/action-ui-check/verification.json`; its seeded action is simulated,
with no device dispatch or recovery claim. The development SQLite database was
backed up, migrated to head and checked for integrity and preserved original rows.

The alternate-identity API test uses timestamp-shifted fixture measurements;
production startup tests use generated model/approval inputs and an injected
SQLite store. These verify contracts and guards, not target-network model accuracy
or defence acceptance. The next priority is reducing and re-evaluating false
alerts with substantially longer independent negative exposure; vendor integration and customer
accreditation follow the applicable real requirements.

Order 5 controls implemented: bounded HTTP admission, authenticated operational
metrics, alert rules, guarded telemetry retention, resource budgets and operating
procedures. Release preflight requires a complete integrity-bound acceptance
record; its template is intentionally pending. These controls do not replace
actual capacity, restore, credential-rotation, isolation or security review drills.
Native LightGBM imports now pass in the built read-only image as UID 10001.

The owned live pilot now passes keyboard login, session retention, incident-detail,
grounded copilot, desktop/mobile and logout checks. Its bounded real-measurement
campaign completed with 15 impacted and 12 normal runs. The untouched test gate
failed precision and false-alert requirements, so the generated candidate remains
unapproved and inactive. Persistent services restore the owned lab,
rehash interrupted campaign evidence, resume at unused seeds and stop on bounded
attempt exhaustion. Neither service grants model approval.
Interrupted export/training attempts are now retained and checksum-referenced;
only derived staging copies are cleared before a retry from the sealed cohort.
The backup mechanism now shares one exported PostgreSQL snapshot between archive
and counts. The real owned archive restored in an isolated PostgreSQL 17 instance
with all ten table counts and schema matching. See [owned-pilot.md](owned-pilot.md)
for service names, evidence and remaining gates.

The v3 campaign completed a fresh 18-runs-per-outcome cohort. Validation passed
the declared 120-second incident constraints: precision/recall 1.0/1.0, zero
false-alert episodes and 97.40 seconds median useful lead. On untouched test,
precision/recall remained 1.0/1.0 and median useful lead was 102.08 seconds, but
one false-alert episode over 0.0434 eligible negative link-hours yielded 23.05
alerts/hour against the 1.0 maximum. The technical gate failed. The excluded demo
holdout reported 1.0/1.0, zero false-alert episodes and 111.35 seconds median lead.
Exporter, trainer, cohort and outputs are hash-bound in the protected evaluation
record. The candidate remains unapproved, unscoped and inactive and must not be
tuned against the revealed test/demo results.

V4 collection is now active under separate root-owned collector/evaluator
releases, with 18 runs per class, base seed 2050000 and a 600-sample normal-soak
baseline. Whole normal runs are assigned until validation, test and demo holdout
each contain at least three eligible negative link-hours while six or more normal
training runs and all traffic profiles remain. Approval/runtime loading requires
the exact `technical-evaluation-v2` policy, every named check, and a one-sided 95%
Poisson false-alert-rate upper bound at or below 1.0 per hour. Services cannot
self-approve or activate the resulting candidate. A frozen-runtime import check
caught an evaluator v11 dependency mismatch before collection completed. The
root-owned v12 evaluator carries its gate constants directly, passed the same-image
import check, and replaced the waiting evaluator without restarting collection.

The hardened PostgreSQL candidate upgrades the available Alpine security fixes,
removes the vulnerable `gosu` helper and runs as UID/GID 70. It currently has no
Trivy findings, passed fresh rootless initialization, passed the six application
PostgreSQL integration tests and restored the sealed backup under network
isolation. The Alpine lifecycle-coverage warning and the remaining API/Ollama
high advisories still block a clean production security gate.
The shell-free distroless API candidate passed its constrained runtime checks and
reduced API high findings from 44 to 21, all reported without a fixed version.
Its offline scan also produced a 75-component CycloneDX SBOM. CI now builds and
executes this path, but it remains unapproved under the zero-high gate.
The official Ollama 0.34.2 amd64 image retained the same 43 high Go/module
findings when scanned by immutable digest, so it was not promoted.
The WSL pilot now measures both host VHD capacity and Linux state-filesystem
capacity every five minutes, atomically records mount/read-write status, and
fails the scheduled check below a 20 GiB floor. Operator alert delivery still
requires the eventual unattended-host monitoring integration. A separate
five-minute watchdog now validates the registered keeper action, restarts a
stopped keeper and records a checker-hash-bound atomic report.
