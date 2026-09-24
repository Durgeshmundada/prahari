# Production readiness gate

Last integration review: 2026-09-22. The repository has implemented security and
workflow controls, but a production rollout has not been certified. Scope remains
one configured monitored path. The bundled Branch-01 topology is a development
fixture; external live deployments supply a validated network profile.

## Implemented

- Deployment-specific site/device/interface/path identities and policies through
  `PRAHARI_NETWORK_PROFILE_PATH`; topology/configuration references fail closed.
  Production requires a production-scoped profile and rejects the lab adapter.
- Predictive approval records bind the canonical network-profile checksum and
  deployment scope. Runtime also checks sampling/window/minimum-sample alignment.
  External copilot context uses only checksum-bound, explicitly reviewed documents.
  Backup release identity includes the network-profile file checksum.
- Versioned telemetry and predictive feature windows, model approval checks,
  provenance/freshness gates, and explicit prediction-unavailable responses.
- Durable review, closure snapshots, independent final case decisions, and audit
  transactions. Historical decisions cannot approve a newer closure submission.
- Client prediction preconditions and action-agent dry-runs keyed to the exact
  prediction. Independently approved, compatible cases enter bounded copilot
  context with historical-outcome labels and snapshot hashes.
- Offline browser accounts, password hashing, persistent sessions, CSRF/origin
  checks, role enforcement, login throttling, logout, and credential revocation.
  Machine API keys remain separate from browser credentials.
- Dashboard sign-in and case review, server-assigned actors, runtime API response
  validation, bounded requests, and stale-evidence UI controls.
- A narrow lab action-agent client and serialized allow-listed route operations,
  review prerequisites, and atomic incident ownership/intent before external
  apply/rollback calls. Each request has a unique operation ID that the adapter
  must echo in its receipt. Unique prediction/kind records prevent repeat
  dispatch after restart. Unconfirmed outcomes block workflow changes and retain evidence.
- Independent action reconciliation with evidence references/checksums, original
  adapter receipts, audited claim release, and competing-decision exclusion.
  Reconciliation requires a monitoring-only API and reviewer attestation that
  all dispatchers are stopped. Route verification does not establish recovery.
  The action-record panel remains available during incident telemetry failures.
- Alembic migrations through `0005_case_search`, least-privilege runtime
  grants, deployment secret checks, backup/restore scripts, and an HTTPS gateway
  example. Restores revoke browser sessions and retain unresolved action claims.
  Migration refuses older uncorrelated device intents pending an approved import
  and reconciliation procedure; automatic schema creation refuses older tables.
- CI jobs for Python, dashboard contracts/components/build, PostgreSQL concurrency,
  Compose security invariants, and image builds.
- Evidence-bound copilot statement selection, exact rendered support and captured
  incident hashes. Live supersession is labelled; unsupported narrative is rejected.
- Evidence reads validate content-derived telemetry, probe, event and audit IDs plus
  normalized case/action metadata. Legacy IDs remain readable under their original
  encoding. A bounded-memory, read-only whole-store verifier reports checked row
  counts and validates incident ownership/current-case references for
  backup/release/restore drills. PostgreSQL verification uses a repeatable-read
  snapshot. This detects row drift and broken workflow references but is not an
  external deletion-proof audit archive.
- Actual-byte HTTP limits and receive deadlines, authenticated operational metrics,
  tested alert rules, guarded maintenance retention, and a mandatory release
  acceptance record bound to images/configuration/models and evidence artifacts.
- Separate web ingress for loopback dashboard publishing; API/database/AI remain
  on internal networks. Ingress disables outbound masquerading. Actual host
  firewall and isolation acceptance are still required.

## Evidence from this workspace

- Latest local check (2026-09-22): the standard backend suite passed 305 tests,
  with six PostgreSQL and two Windows symlink-privilege checks skipped. The six
  PostgreSQL checks then passed separately against an isolated hardened PostgreSQL
  17 container at schema `0005`; the SQLite `0004` to `0005` backfill/index
  upgrade also passes. Ruff passed.
  Dashboard: 28
  component/contract tests passed; TypeScript checks and production build passed.
  A fresh empty SQLite store migrated through `0005` and passed the read-only
  evidence-integrity verifier. The locked Python runtime and dashboard npm audit
  reported no known dependency vulnerabilities. The six Prometheus alert rules
  and their simulations passed with checksum-verified Prometheus 3.14.0 tooling.
  Containerlab validated the nine-node/eight-link topology without deployment;
  preflight passed, and all 32 production Compose mutation checks were rejected.
  Evidence and checksums are under `artifacts/local-acceptance-20260922`.
- PostgreSQL 17 in an isolated WSL Docker test container at schema `0005`: clean migrations,
  concurrent login counters, competing closure decisions, session cap,
  revocation, competing action claims and competing reconciliations passed in
  six integration tests, including retention exclusion during pending dispatch
  and repeatable-read evidence/incident relationship verification before and
  after action reconciliation.
  Tests use random schemas and
  delete only those schemas. CI is configured to repeat them against the pinned
  PostgreSQL 16 deployment image; a configured CI job is not a completed CI run.
- Development and production Compose templates rendered successfully under WSL.
  All 32 production security mutation checks passed. Templates contain inert
  credentials/artifact digests and do not constitute a deployable release.
- Provisioning tests verify account preservation, explicit credential replacement,
  writer exclusion, and preservation of the original file after storage failure.
- Real-browser desktop/mobile action-record checks passed, including visibility
  during an actual incident API 503 and reconciliation attestation controls.
  Screenshots and `verification.json` are under `artifacts/action-ui-check`.
  The seeded pending operation is explicitly simulated; no device call occurred.
  Broader sign-in, keyboard and session-expiry acceptance remains a release gate.
- Owned live pilot browser checks passed for keyboard login, session retention,
  actual incident detail, a grounded copilot brief, desktop/mobile layout and
  logout. Device dispatch is disabled. The measured campaign completed with 15
  impacted and 12 normal runs across all configured traffic/fault profiles. The
  untouched 120-second test gate failed precision (0.667 vs 0.70 required) and
  false alerts (38.81 vs at most 1.0 per eligible negative link-hour); recall was
  1.0 and median useful lead time was 100.15 seconds. The candidate is unapproved,
  unscoped and inactive. The collector supports hash-verified checkpoint resume,
  distinguishes infrastructure outages from measurement failures, and runs
  under persistent Linux supervision. Interrupted model export/training is
  preserved as an attempt and can restart only from revalidated sealed inputs.
  The fresh v3 campaign subsequently completed 18 impacted and 18 normal runs.
  Its validation split passed the 120-second selection constraints with 1.0
  precision/recall, zero false-alert episodes and 97.40 seconds median useful
  lead. The untouched test split retained 1.0 precision/recall and 102.08 seconds
  median lead, but one false-alert episode across 0.0434 eligible negative
  link-hours produced 23.05 alerts/hour against the 1.0 maximum. The technical
  evaluation gate therefore failed. The demo holdout, excluded from the gate,
  reported 1.0 precision/recall, zero false-alert episodes and 111.35 seconds
  median lead. Exporter, trainer, cohort and outputs are hash-inventoried; the v3
  candidate remains unapproved, unscoped and inactive.
  A prospective v4 cohort is now running under new root-owned, hash-separated
  collector/evaluator releases. It uses 18 runs per class, base seed 2050000 and
  a roughly 50-minute normal-soak baseline. Validation, test and demo holdout must
  each receive at least three eligible negative link-hours; activation requires
  the complete 95% Poisson upper-bound gate rather than a bare passed flag.
  A pre-completion compatibility check found that evaluator v11 imported new
  policy constants from the older frozen campaign runtime. Root-owned evaluator
  v12 removes that dependency, passed an import smoke test in the exact worker
  image against the frozen runtime, and replaced only the waiting evaluation
  service; collection continued without restart.
  On 2026-09-22 the four registered Windows host tasks were re-enabled so the
  WSL pilot, capacity check and watchdog remain supervised after interactive
  shells exit. Capacity and watchdog reports are healthy. At the audit snapshot,
  v4 had sealed all 18 impacted runs and had begun the normal-soak phase; two
  earlier samples were rejected because their component timestamps were not causal.
  The collector is retrying and the model remains unapproved. WSL reports active
  synchronized NTP, but Windows reports its clock source as unsynchronized; host
  time synchronization must be corrected before treating long-soak timing as
  acceptance evidence. The causal validator must not be weakened.
  See [owned-pilot.md](owned-pilot.md) and
  `artifacts/owned-pilot-check/verification.json`.
- The corrected backup helper binds archive and table counts to one exported
  PostgreSQL snapshot. A real owned-server backup restored into an isolated
  PostgreSQL 17 container with all ten counts and schema matching in six seconds.
  The source database was not switched. This is a mechanism drill, not HA or
  independent production acceptance; retained evidence includes helper/archive
  hashes in `artifacts/owned-pilot-check/restore-verification.json`.
- Disposable replay browser checks passed actual idle expiry, re-login and
  revocation after account disabling. Production identity-policy/HTTPS acceptance
  remains open. The updated API/dashboard candidates also passed isolated browser
  login, detail, desktop/mobile layout and logout checks.
- All four deployed container images were scanned. High/critical advisories remain
  release blockers; see `artifacts/security-check/owned-containers/summary.json`.
  Updated API/dashboard candidates were built and scanned separately. API critical
  findings are removed, with 44 high advisories remaining; the dashboard scan
  reports no findings and an Alpine EOL-metadata coverage warning. A hardened
  PostgreSQL 17 candidate also reports no current findings, runs as UID/GID 70
  without `gosu`, passed fresh initialization and all six PostgreSQL application
  integration tests, and restored the sealed backup with exact schema/counts.
  Alpine coverage still needs review. This is not an independent security review
  or a clean production release.
- A shell-free distroless API candidate passed numeric-user, read-only-root,
  no-network, no-new-privileges, dropped-capability, native-import and migration
  checks. Its offline Trivy 0.74.0 scan produced a 75-component CycloneDX SBOM
  and reduced high findings from 44 to 21. All 21 are Debian/Python runtime
  advisories with no fixed version reported by that database. CI now builds and
  executes this candidate, but it remains unapproved under the zero-high gate.
- The official Ollama 0.34.2 amd64 candidate was resolved and scanned by immutable
  digest. It retained 43 high Go/module findings, so it was not promoted. During
  the large pull, the C:-backed WSL VHD exhausted its host filesystem and ext4
  remounted read-only. Moving the stopped distro to D: restored read-write service
  and the measured campaign resumed from its sealed checkpoint. A five-minute
  task now records the VHD backing-drive free space, Linux state-filesystem free
  space and root mount mode, with a 20 GiB floor and failing task result. Alert
  delivery and an independent filesystem recovery drill remain open. A separate
  five-minute watchdog validates the registered keeper action and restarts a
  stopped keeper; its atomic report binds the checker SHA-256.
- The existing development SQLite database was backed up, upgraded through
  `0005_case_search`, and checked for integrity and preservation of original row
  counts. The pre-`0005` backup is
  `artifacts/prahari-pre-0005-20260920T203037Z.db` with SHA-256
  `938d7c1989a32dbf3fd8dec500f4f4c500786b2beb97f95d6ee60fa4e47b7fcc`.
  Its earlier migration backup contained no uncorrelated legacy device intents. This does not
  replace a production-image migration/grant/restore rehearsal.

## Open implementation work

The external profile increment is implemented and tested. Multiple concurrent
incident streams, target-vendor collectors and the external validation/dataset
pipeline still need implementation or target-environment integration; the current
predictive observation collector/dataset contracts remain tied to the controlled
lab experiment. Configuring different identities does not prove model generalisation.

1. Add approved vendor dispatch fencing and target-device failure acceptance.
   Database workflow ownership, current-review checks and durable reconciliation
   are implemented; external device operations do not freeze telemetry, and
   cross-worker quiescence during reconciliation remains reviewer attestation.
2. Extend the verified statement catalog only when new claim kinds have typed
   support; retain independent review of procedural content and model acceptance.
3. Integrate independently collected target-device evidence with the automatic
   application-probe recovery comparison. The API now captures bounded before/after
   samples across every impacted application stream, validates a sustained
   degraded-to-within-policy transition, and self-verifies a v2 evidence SHA-256
   that binds captured thresholds, window verdicts and maxima; vendor route
   evidence is still separate.

Indexed case search is implemented through `0005_case_search`: review status,
outcome, incident type, entity and network-profile hash are stored in indexed
columns, backfilled from retained payloads, exposed by the API and dashboard, and
kept separate from the immutable case snapshot. Copilot retrieval ranks exact
scope first and admits related scope only with matching approved profile/model
hashes and overlapping applications.

## Deployment release gates

Record target-environment evidence before connecting production devices:

1. Configure actual identities, managed secrets, HTTPS hostname/certificates,
   and owners; rehearse account rotation, revocation, and incident response.
2. Rehearse production-image migrations, least-privilege grants, backup/restore,
   retention, capacity, failure handling, monitoring, and rollback.
3. Approve the actual model, dataset, calibration, thresholds, target scope,
   false-alert and lead-time results, and rollback artifacts. Lab approval does
   not authorize production deployment.
4. Run a fresh controlled Linux network-lab acceptance sequence covering traffic,
   fault, mitigation, observed recovery, rollback, and zero-egress evidence.
5. Complete real-browser checks of sign-in, keyboard access, responsive layout,
   reviewer separation, error states, and session expiry.
6. Complete dependency/container scans and an independent security review.

Account provisioning and deployment steps are in [deploy/README.md](../deploy/README.md).
The supported workflow and known limits are in
[api-and-ui-contract.md](api-and-ui-contract.md).
