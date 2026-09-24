# API and UI contract

## Goal

The dashboard must show traceable evidence and human-control states, not only a red/yellow/green score.

## Deployment profile

Live deployments can supply `PRAHARI_NETWORK_PROFILE_PATH`. The validated
`network-profile-v1` contains site/device/interface inventories, paths and probe
entity mappings, policy versions, the selected monitored entity/incident and a
monitoring-only playbook. Scope is one selected monitored path per runtime.
Invalid or unknown references fail startup; ingestion rejects the wrong entity,
path, topology version, configuration version or probe mapping. External profiles
cannot use replay fixtures or the controlled-lab device adapter.

`GET /api/topology` adds nullable `profile_id` and `profile_sha256` metadata.
Predictions add nullable `network_profile_sha256` and `model_bundle_sha256`;
both enter the prediction ID and immutable case snapshot. A deployment profile
or model-bundle change invalidates prior reviews even when its telemetry versions
and computed probabilities are unchanged. Historical case retrieval also requires
these bindings to match.
The latter hashes the canonical validated profile including defaults, policies
and the approved runbook catalog. Production requires explicit production scope,
an activated forecasting bundle approved for this checksum/scope and matching
window policy, and a read-only approved knowledge directory. The external copilot
loads only catalog documents whose exact bytes match their reviewed checksums;
its configured playbook source must be in that catalog. The copilot selects
server-issued statement IDs; it cannot write narrative claims or confidence.
The server verifies support against the captured incident and typed historical
outcomes before rendering the brief. Catalog approval does not establish the
truth of procedural prose or defence accreditation.

The current evidence schema retains normal/tunnel-degradation classes and
30/60/120-second horizons with loss/jitter and chat/file-upload measurements.
Changing those protocol contracts requires separate model/runtime validation.
Deployment instructions and checksum tooling are in
[deploy/README.md](../deploy/README.md).

## Core API resources

| Resource | Purpose |
|---|---|
| `GET /api/topology` | Current versioned nodes, links, paths, site mapping, and health overlays. |
| `GET /api/metrics` | Time-series evidence for an entity/path and requested window. |
| `GET /api/incidents` | Current card for the configured monitored path. |
| `GET /api/action-operations` | Bounded durable action records and whether this API instance permits device dispatch; readable without fresh telemetry. |
| `POST /api/action-operations/{id}/reconcile` | Independent reviewer records verified route evidence after dispatch is disabled and workers are quiesced. |
| `GET /api/incidents/{id}` | Current prediction, feature window, playbook, impact status, and recent audit trail. |
| `POST /api/incidents/{id}/brief` | Generate a cited local copilot brief (operator/admin). |
| `POST /api/incidents/{id}/review` | Bind this operator's review to the current prediction evidence. |
| `POST /api/incidents/{id}/actions/{playbook_id}/dry-run` | Request validation for an approved playbook. |
| `POST /api/incidents/{id}/actions/{playbook_id}/approve` | Explicitly record authorized action after successful validation. |
| `POST /api/incidents/{id}/actions/{playbook_id}/rollback` | Request an audited emergency restoration of the primary route. |
| `POST /api/incidents/{id}/closure` | Submit an outcome for the reviewed prediction; successful outcomes await independent review. |
| `POST /api/cases/{id}/review` | Approve/reject the current closure submission (reviewer/admin). Here `{id}` is the incident ID. |
| `GET /api/models` | Active model version and evaluation summary. |
| `GET /api/audit/{id}?limit=200&offset=0` | Read recorded events even when prediction is withheld (reviewer/admin). |
| `GET /api/auth/session` | Browser session status, authenticated actor, CSRF token, and hard expiry. |
| `POST /api/auth/login` | Sign in with a provisioned local account and rotate the session cookie. |
| `POST /api/auth/logout` | Revoke the current session (CSRF protected). |
| `GET /api/cases?status=pending&outcome=resolved&incident_type=tunnel_degradation&entity_id=...&network_profile_sha256=...&limit=20&offset=0` | Browse durable closure snapshots through indexed, optional applicability filters. |
| `GET /api/cases/{case_id}` | Retrieve a closure snapshot and its independent review. |
| `POST /api/cases/{case_id}/decision` | Record one final independent decision against that exact case. |

Every mutation needs authenticated actor identity, timestamp, incident/playbook/model version, and audit record.

## Implemented MVP workflow

The service currently manages one path under `inc-branch-01-primary`. Durable
closure cases preserve historical submissions and evidence for this path.
The copilot can retrieve independently approved cases with matching path scope,
incident type, model version, and location. This is bounded historical context,
not a general historical-incident search API.
Prediction IDs are opaque hashes of the feature window and scored prediction,
including its model version. Clients must compare the complete ID rather than
parse a timestamp from it. New prediction evidence invalidates an older review
and closure; the path can raise an incident again.

Review, dry-run, approve, and closure requests accept `prediction_id`. The
dashboard always supplies the complete ID shown in its evidence panel. In
production, omission returns HTTP 428; an ID that differs from current evidence
returns HTTP 409 before the operation proceeds. Development/test clients can
omit it for compatibility. The action-agent protocol requires `prediction_id`
and a unique `operation_id`; the agent must echo that operation ID in its receipt.
Successful dry-runs are keyed by incident, playbook, actor, and prediction.
Update the API and agent together: older request protocols are no longer
accepted. Rollback remains an emergency operation without this client
precondition. The API rechecks evidence after recording apply intent; these
checks do not freeze telemetry throughout an external route operation.

Review, dry-run, approval, rollback, and closure submission require an operator
or admin role. With API-key authentication enabled, the request's `actor_id`
must match the authenticated principal. The local auth-disabled mode supports
development identity headers; it is not a production identity mechanism.

Dry-run and traffic-shift approval require this operator's review of the current
prediction within five minutes. The isolated action agent also requires a fresh,
successful dry-run and serializes dry-run, apply, and rollback operations.
Apply and rollback record intent before calling the agent and record the
reported outcome afterward. A failed agent request returns HTTP 503: the route
change may have occurred, so inspect the audit and observed routing before
retrying. Rollback remains an emergency operator operation without a prior
incident-review requirement, subject to live telemetry readiness and agent checks.

Closure submission accepts `actor_id`, `outcome`, and `note`. It requires the
submitter's current, recent review. `resolved` and `false_positive` submissions
are blocked while sustained application impact is present, and otherwise enter
`validating`. `partially_resolved`, `failed`, and `uncertain` enter `escalated`.
An independent reviewer then calls:

~~~json
{
  "actor_id": "reviewer-01",
  "decision": "approved",
  "note": "Application checks and closure evidence independently reviewed."
}
~~~

`decision` is `approved` or `rejected`. The reviewer must be a different actor
from the submitter. Missing or outdated closure evidence, self-review, and
approval during current application impact return HTTP 409. An approved
`resolved` or `false_positive` outcome becomes `resolved`; rejection keeps the
incident `escalated`. Renewed application impact always takes precedence and
raises severity to `high`, even after an approved closure. A live `resolved`
submission must include an offset-aware `intervention_at`. The API automatically
selects bounded, healthy, topology-matched application probes from the configured
window before and after that instant. It requires sustained pre-intervention
impact and sustained post-intervention within-policy measurements, then preserves
the exact samples, maxima, policy version and canonical SHA-256 in the closure
case. New evidence uses `recovery-comparison-v2`, whose hash binds the policy
version, captured loss/jitter thresholds, required sustained duration, derived
window verdicts and maxima as well as the path, intervention and samples. The
reader retains `v1` compatibility for existing cases, but a reviewer cannot
approve legacy evidence because its older hash did not bind those fields. A replay
fixture may omit this field, but that case is explicitly fixture
evidence and is not production recovery proof.

Review and closure state are persisted together with their audit entries. The
existing storage columns contain versioned review/closure metadata. Migration
`0002_closure_cases` adds historical snapshots and `0003_browser_sessions` adds
sessions and shared login counters. Legacy notes remain readable but cannot authorize actions
without a new review bound to current evidence.

Approved incident memory uses typed applicability. Exact entity, location, scope,
network-profile and model-bundle matches rank first. A related-scope case is
eligible only when non-null approved profile and model-bundle hashes match and the
application sets overlap; its evidence states every differing dimension and gets
a lower score. Free-text operator and reviewer notes remain excluded from copilot
context, and historical outcomes never establish current recovery or permission.

Each new closure has an opaque `case_id`, the submitted prediction, incident
card, feature window, impact status, submitter, and timestamp. The dashboard
uses `/api/cases/{case_id}/decision`; a final decision is immutable and an audit
entry commits in the same transaction. Reviewing an older case cannot approve
a newer submission. Historical case decisions can be made after evidence changes;
approval of the current successful closure still requires readiness and no current
application impact. Self-review is prohibited even for administrators. Case list
limits are 1–200 (default 100), with non-negative offsets, newest first.

Audit reads return the newest 200 events by default, with a maximum `limit` of
1000 and a non-negative `offset`. The offset counts from the newest event; each
returned page is ordered oldest to newest for display. New inserts can shift
offset pages, so pagination is not a frozen export. Incident detail includes at
most the newest 1000 audit events. Stored audit history is retained. Telemetry,
application-probe, event and audit reads recompute their content-derived IDs and
verify indexed metadata before returning evidence. IDs written before run/context
binding remain verifiable through their documented legacy encoding. These checks
detect changed rows; they do not prove that a privileged database administrator
has not deleted a complete row, so backup and external retention controls remain
part of release acceptance.

The dashboard refreshes selected incident evidence on each polling cycle while
preserving action feedback. It only displays a generated brief for matching
prediction evidence, and enables traffic-shift approval after a successful
safety check for that evidence and operator. The API remains responsible for
enforcing review and action-agent prerequisites.

## Browser identity and response validation

Local browser authentication is configurable and requires persistent storage,
API-key mode, provisioned accounts, and an exact same-origin URL. Production
requires HTTPS and distinct active operator/reviewer identities. API keys remain
available for machine clients and are not embedded in dashboard assets or storage.

Successful sign-in sets an HttpOnly, SameSite=Strict cookie; HTTPS uses Secure
and the `__Host-` prefix. Unsafe cookie-authenticated requests require the exact
configured Origin and `X-CSRF-Token`. The dashboard obtains that token from the
session response and retains it in memory. Actor IDs are displayed read-only;
the server enforces the authenticated role and rejects actor mismatches.

Sessions have an eight-hour maximum lifetime and a configurable idle timeout
(default 30 minutes). At most five sessions per username are retained. Login
limits are persisted across workers: 60 attempts per minute globally and ten
per five minutes in each of 256 username hash buckets. Accounts sharing a bucket
share its limit. Disabled accounts and changed credentials/roles invalidate
sessions when the updated account file is visible to the API; see deployment
instructions for file-mounted secret rotation. Session endpoints return no-store.

Dashboard API responses pass runtime Zod validation before rendering. Malformed
success responses become explicit errors. Requests have bounded timeouts;
HTTP 401 expires the visible authenticated workspace. Component tests cover sign-in,
sign-out, closure review, malformed responses, and stale-evidence UI controls.

## Remaining delivery work

- Replace or revise the v3 challenger through a new predeclared experiment. Its
  validation split passed, but untouched test produced one false-alert episode
  over 0.0434 eligible negative link-hours: 23.05 alerts/hour against the 1.0
  maximum. Do not tune v3 against the revealed test/demo holdouts. Substantially
  longer independent normal exposure and accountable approval are still absent;
  the runtime remains heuristic/unapproved.
- Capacity, restore, isolation, session-expiry and independent security acceptance.

## Live incident and brief contracts

`run_manifest` is discriminated by `fixture_only`. Replay manifests retain the
scenario, random seed and expected-outcome metadata. Live manifests contain
`run_id` and `fixture_only: false`; measured timestamps and quality remain in
`feature_window`. A live response must not invent replay labels or seeds.
`device_dispatch_enabled` reports actual adapter availability. Missing metadata
defaults to disabled in the client; review and evidence capture remain available.

`POST /api/incidents/{id}/brief?prediction_id={selected_prediction}` rejects an
already superseded request with HTTP 409. `grounding` includes the full immutable
incident snapshot, its SHA-256, and every statement's support path, source hash,
kind and content-derived claim ID. Narrative and citation text must match those
verified statements exactly. Human case notes remain reviewable through the case
API but cannot supply generated claims or model instructions.

Collection continues during generation. `evidence_current_at_completion` is true
only when the completed brief still matches the current incident snapshot. An
older verified snapshot is returned with false and visibly labelled in the UI,
including its prediction ID. This never authorizes a review, closure or device
operation for newer evidence; those retain independent prediction preconditions.

Authenticated internal `GET /metrics` exposes bounded operational labels,
request-duration histograms, storage/readiness state, unconfirmed-action counts
and copilot results. The public dashboard gateway does not proxy it. HTTP bodies
are bounded by actual received bytes and a total receive deadline; 413/408 occur
before mutation. See [OPERATIONS.md](../deploy/OPERATIONS.md) for maintenance
retention and the mandatory evidence-bound release acceptance record.

The operator evidence-integrity command validates retained record payloads and
normalized metadata, then checks workflow relationships: a pending or unknown
action must be the active owner of its incident, and a current closure-case ID
must resolve to matching incident, prediction, submission and review evidence.
PostgreSQL checks use one read-only repeatable-read snapshot. The command reports
row counts; it cannot prove the absence of deletion for an unreferenced row.

## Approved incident-memory context

When the local copilot is enabled, its runtime provider reads the newest 100
approved closure cases and adds at most two compatible cases to retrieved
runbook context. Pending, rejected, self-reviewed, missing-review-time,
poor-quality, and mismatched cases are excluded. Matching requires the same
prediction entity, incident type, model version, affected scope, and root entity.
Source IDs are `case:{case_id}`, with an exact snapshot hash and API source path.

Historical notes retain the submitted outcome, including failure or uncertainty;
review approval does not turn these into successful resolutions. The prompt
identifies notes as historical data rather than instructions or current
observations. Citation IDs are constrained to retrieved sources. Case retrieval
failure is explicit; no unreviewed fallback is used. Semantic claim-support
verification remains an open requirement, and cases are not automatically
promoted into model-training labels.

## Incident card shape

~~~json
{
  "id": "inc-101",
  "state": "predicted",
  "severity": "high",
  "summary": "Primary Branch-01 path likely to cause video impact in 1-2 minutes.",
  "prediction": {
    "prediction_id": "pred-risk-lgbm-horizons-v2-1700000000",
    "model_version": "risk-lgbm-horizons-v2",
    "entity_id": "tunnel:branch-01-to-dc-primary",
    "likely_incident_type": "tunnel_degradation",
    "incident_type_probabilities": {
      "normal": 0.16,
      "tunnel_degradation": 0.84
    },
    "impact_risk": {
      "within_30_seconds": 0.36,
      "within_60_seconds": 0.61,
      "within_120_seconds": 0.84
    },
    "time_to_impact": {
      "range": "60-120 seconds",
      "basis": "first approved horizon whose calibrated risk crosses its frozen threshold"
    },
    "confidence": 0.78,
    "risk_status": "calibrated",
    "data_quality": "good",
    "root_location_candidates": [
      {
        "entity_id": "interface:branch-edge:eth1",
        "site_id": "site:branch-01",
        "score": 1.0,
        "rationale": "Controlled topology manifest maps the primary path to branch-edge eth1."
      }
    ],
    "affected_scope": {
      "paths": ["path:branch-01-to-dc:primary"],
      "applications": ["chat", "file-upload"],
      "sites": ["site:branch-01", "site:dc-01"],
      "backup_path_id": "path:branch-01-to-dc:backup"
    },
    "evidence": [
      "same-path network and application observations passed freshness and provenance checks",
      "120-second calibrated risk crossed the approved model threshold"
    ]
  },
  "location": {
    "entity_id": "interface:branch-edge:eth1",
    "site_id": "site:branch-01",
    "score": 1.0,
    "rationale": "Controlled topology manifest maps the primary path to branch-edge eth1."
  },
  "data_quality": "good",
  "evidence_ids": ["metric-window-123", "event-87"],
  "playbook_status": "available_pending_review"
}
~~~

## Required dashboard views

| View | Must show |
|---|---|
| Topology | Primary/backup path, device/site labels, current risk overlay, selected affected scope. |
| Incident card | What, where, when, confidence, data quality, evidence, and state. |
| Evidence panel | All 30/60/120-second risks, ETA and basis, metric trends, events, app-probe health, source timestamps, data-quality limitations, and model version. |
| Copilot panel | Cited summary, approved playbooks, uncertainties, no raw auto-executable commands. |
| Action panel | Dry-run state, approver, validation output, execution/rollback status. |
| Feedback panel | Caretaker closure form and reviewer status. |
| Audit/model panel | Active model/dataset/feature version and decision history. |

## UI states

~~~text
healthy → watch → predicted → reviewed → mitigating → validating → resolved/escalated
~~~

The UI must visibly distinguish:

- a trend observation from a prediction;
- a prediction from confirmed root cause;
- a recommended playbook from an approved action;
- an approved action from a validated recovery; and
- a draft feedback note from a reviewed incident-memory case.

## Error and fallback behavior

Action mutations register a prediction snapshot, unique operation ID and audit
intent atomically with an incident claim. One unconfirmed operation may own an
incident. The current operator review and its five-minute freshness gate are
checked in that transaction. While owned, a new review cannot replace the
authorization and closure cannot erase uncertainty. A superseded operator review
also prevents saving a closure snapshot in the final database transaction.

Operation states are `pending` (durable intent, no confirmed result), `unknown`
(timeout, invalid reply or failed validation), and `finished` (confirmed adapter
result or independently reconciled route state). A crash may leave `pending`;
neither expiry nor restart establishes that no command ran. Every new adapter
receipt must match the unique requested operation ID, and responses reporting
success must match the expected observed route. Storage failure after a reply
returns HTTP 503 and preserves the unconfirmed claim. Apply/rollback requests
for the same incident/playbook/prediction/kind cannot dispatch twice, including
across restarts. The platform never automatically retries uncertain operations.

`GET /api/action-operations` returns up to 100 records and `dispatch_enabled`.
The dashboard loads this separately from incident predictions, so a telemetry
503 cannot hide an interrupted operation. `workflow.unconfirmed_action` disables
review, safety-check, approval, rollback and closure controls.

Reconciliation is restricted to reviewer/admin and a different actor from the
requester. This API instance must have its device adapter disabled. The request
includes `observed_route`, `evidence_reference`, a non-placeholder
`evidence_sha256`, a verification note and `dispatch_quiesced: true`. Stop every
dispatch worker and the device agent, restart the API in monitoring mode, then
verify the actual device route independently before recording the result.
Cross-worker quiescence is an accountable reviewer attestation, not an automatic
proof or downstream fencing implementation. Vendor-adapter fencing remains a
release requirement before enabling production mutation.

Reconciliation preserves the original `agent_receipt` and prediction snapshot,
records the reviewer observation separately, and releases the incident claim in
one audited transaction. A competing second decision cannot replace it. Verified
route state does not imply application recovery or authorize a closure. Restores
retain operation records and unresolved claims; they never replay commands.

| Condition | UI behavior |
|---|---|
| Telemetry stale/missing | Mark data quality degraded; avoid high-confidence prediction. |
| Predictive model absent/unapproved or evidence join invalid | Withhold calibrated risk, expose the reason, and never silently substitute a heuristic as an approved prediction. |
| Incident prediction cannot be produced | List and detail return HTTP 503 with the reason; the dashboard shows an error rather than a healthy empty list. |
| Topology mapping unavailable | Show entity only; do not invent a site/path. |
| RAG has no approved match | Show model evidence and “manual investigation required.” |
| Local LLM invalid/unavailable | Show structured card and source results; do not hide the failure. |
| Dry-run fails | Block approval and show validation evidence. |
| Action-agent response unavailable/invalid | Show HTTP 503 and retain durable intent; do not infer that no route change occurred. |
| Recovery windows are missing, discontinuous, oversized, not previously impacted, or still breach policy | Reject a live `resolved` submission; retain the incident for investigation. |
