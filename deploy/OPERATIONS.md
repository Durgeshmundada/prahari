# Production operating procedures

Scope is one configured monitored path, with monitoring-only production playbooks.
Customer-controlled dispatch requires a separately implemented and accepted vendor
adapter. These procedures and their examples are not deployment approval.

## Monitoring

`/metrics` requires an authenticated viewer/operator/reviewer/admin when API auth
is enabled. Use a dedicated machine viewer token from a protected file; do not put
browser credentials or bearer tokens into URLs. The dashboard gateway deliberately
does not publish `/metrics`. Scrape from an approved internal monitoring network.

Configure `monitoring/prometheus.example.yaml` for the actual targets, token file,
customer-controlled Alertmanager and TLS/CA when crossing hosts. Scrape settings
follow the [Prometheus configuration contract](https://prometheus.io/docs/prometheus/latest/configuration/configuration/).
The example has no Alertmanager destination and sends no notifications on its own.
Review intervals/error thresholds against the customer workload and detection budget.

`prahari_database_available` and `prahari_prediction_ready` report observed health.
`prahari_unconfirmed_actions` reports the incident ownership claim: 1 or 0. A storage
failure returns NaN for action state, never a healthy zero. HTTP counters/durations
use route templates, bounded methods and status codes; no entity IDs, actors,
notes, tokens or raw unmatched paths enter labels. Copilot outcomes count verified
briefs and unavailable generation attempts.

Six example [alert rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
cover scrape failure, storage, withheld predictions, unconfirmed operations, API
errors and repeated copilot failures. Validate before deploying:

```bash
bash deploy/scripts/verify-monitoring.sh /approved/tools/promtool
promtool check config --syntax-only deploy/monitoring/prometheus.example.yaml
```

Counters/histograms are process-local. The reviewed production Compose contract
requires one Uvicorn worker, bounded concurrency (default 64, maximum 256), and a
five-second idle keep-alive. Scrape each API instance separately if using multiple
instances. HA, load-balancing, database failover and measured capacity require a
target-environment design and drill; this template does not establish HA.

## Request and capacity limits

HTTP bodies are bounded before JSON/multipart parsing, including actual chunked
bytes. Default `PRAHARI_HTTP_MAX_BODY_BYTES` is 2 MiB; the validated setting allows
64 KiB through 4 MiB. `PRAHARI_HTTP_BODY_TIMEOUT_SECONDS` bounds total receipt time
(default 15 seconds). Bodies return 413/408 when oversized/too slow and retain
request IDs/security headers. Development traffic uploads have a separate file
bound plus 64 KiB multipart overhead. Production forbids that traffic generator.
Collectors should send bounded batches and investigate 413/408 rather than retry
the same oversized request indefinitely.

The local model uses finite context/output limits, disables thinking for statement
selection and bounds its response to 1 MiB. Models incompatible with this contract
fail visibly. The protocol follows the [Ollama chat API](https://docs.ollama.com/api/chat)
and [thinking controls](https://docs.ollama.com/capabilities/thinking). Measure cold
and warm latency on the selected hardware; unit tests and a small replay smoke
test are not sustained-load or target-network acceptance.

## Unconfirmed action and recovery

Do not retry or infer that no command ran after a timeout, process death or result
storage failure. Keep the database/evidence, stop every dispatch-capable API and
device agent, restart the API in monitoring mode, and independently inspect the
route. An authenticated reviewer different from the requester records the observed
route, retained artifact reference/checksum and dispatch-quiescence attestation.
This releases the claim atomically with its audit record; original adapter receipts
remain separate from reviewer observations. It does not establish application
recovery. Follow the procedure in [README.md](README.md) and the API contract.

## Retention

The API never prunes operational evidence automatically. Select the customer
retention policy and preserve a verified backup before a maintenance run. Use an
approved maintenance-owner database credential, not the restricted runtime app
credential. The runtime app receives no telemetry DELETE grant.

```bash
uv run python scripts/prune_telemetry.py --database-url-file /protected/maintenance-url \
  --retain-days 30 --batch-size 1000
```

This previews eligible counts only. Explicit execution adds `--execute --actor-id
<accountable-maintainer>`, with bounded `--max-batches` (default one). Each batch
locks against incident ownership changes and audits its deletions in the same
transaction. The result and audit event record a SHA-256 over each table's ordered
deleted row IDs so the bounded removal can be reconciled without copying the IDs
into the log. An unconfirmed action blocks pruning. Only non-training telemetry,
probes and events with both observation and ingestion older than the cutoff qualify.
At least the most recent 24 hours are preserved. Training run records, cases,
actions, workflow state and audit history are retained. Legal holds, archived
training data removal and case/audit retention need a separately approved policy.
Preview can be stale; execution rechecks claims inside the guarded transaction.

## Evidence integrity verification

Run a complete read-only evidence scan before backup, release acceptance and after
restore. Supply a protected read-capable database URL file; the command never
prints the URL:

```bash
uv run python scripts/verify_evidence_integrity.py \
  --database-url-file /protected/app-database-url --batch-size 1000
```

The verifier scans telemetry, application probes, events, audit entries, closure
cases, action operations and incident states in one database transaction. On
PostgreSQL it uses a read-only repeatable-read snapshot. It validates each
payload, recomputes its current or documented legacy content ID, checks normalized
metadata, and verifies that every unconfirmed action owns its incident and every
current closure-case reference resolves to matching evidence. A successful v2
JSON result contains the verified row counts. This detects changed rows and broken
workflow references but does not prove that a privileged administrator did not
delete a whole unreferenced row; compare the counts with the consistent backup
manifest and external retention records.

## Consistent backup

The backup helper holds an exported repeatable-read PostgreSQL snapshot while
both row counts/schema revision and `pg_dump --snapshot` are collected. This
prevents concurrent ingestion from making the restore manifest inconsistent with
its archive. PostgreSQL requires the exporting transaction to remain open until
the dump imports its snapshot; see [snapshot synchronization](https://www.postgresql.org/docs/17/functions-admin.html#FUNCTIONS-SNAPSHOT-SYNCHRONIZATION)
and [pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html). Query and dump
deadlines close the exporting session on failure. Keep source migrations paused
during backup; this is logical backup, not point-in-time recovery or HA.

## Hardened PostgreSQL candidate

`postgres.Dockerfile` starts from the pinned PostgreSQL 17 Alpine image, installs
exact fixed crypto/UUID packages, removes `gosu`, and sets the runtime user to the
base image's verified UID/GID 70. Verify a newly built local candidate before it
can enter a release manifest:

```bash
sudo bash deploy/scripts/verify-postgres-image.sh LOCAL_CANDIDATE_REFERENCE
sudo bash deploy/scripts/scan-local-image.sh OWNED_STATE LOCAL_CANDIDATE_REFERENCE REPORT_NAME
sudo bash deploy/scripts/verify-postgres-restore-image.sh \
  LOCAL_CANDIDATE_REFERENCE VERIFIED_BACKUP_DIRECTORY
```

The initialization and restore checks use a disposable labelled volume, a
read-only root filesystem, no Linux capabilities, no network for restore, and
remove only their own labelled resources. The scanner runs without a Docker
socket, network access or application secrets. Each candidate scan also creates
a checksum-bound CycloneDX component inventory beside the vulnerability report.
New scan verification records bind the exact offline vulnerability database and
metadata SHA-256 values plus its update/download timestamps; refresh that database
through the reviewed online preparation step, never from the isolated worker.
A local image ID is evidence for that host; a
release still needs a published immutable registry digest, refreshed advisory
data, lifecycle coverage review and independent security acceptance.

## Release acceptance

Complete `release-acceptance.template.json` with actual customer requirements,
test artifacts and an independent accountable approval. It is intentionally
incomplete and cannot pass validation. Provision `PRAHARI_ACCEPTANCE_EVIDENCE_DIR`
and `PRAHARI_ACCEPTANCE_REPORT_FILE` in the production environment file.

```bash
uv run python deploy/scripts/validate-release-acceptance.py deploy/.env.production \
  --print-release-identity
uv run python deploy/scripts/validate-production-artifacts.py deploy/.env.production
```

The identity binds image digests, exact profile/model/Ollama artifacts and Compose
files, using the same identity as backups. Required acceptance categories cover
collection, model validation, restore, capacity/failure, browser access,
credential rotation, isolation, vulnerability scans, independent security review
and operating ownership. Additional customer requirements may be included.
Pending/failed, expired, future-dated, incomplete or release-mismatched records
fail. Artifacts must be nonempty, checksum-matched, inside the evidence root and
free of symlink traversal. JSON duplicates and placeholder hashes fail.

The validator establishes coverage, bindings and file integrity. It does not
verify the truth of a test result, reviewer authority, digital signatures or
defence accreditation. Those require the customer-controlled approval process.
Do not use generated unit-test acceptance/model fixtures as customer evidence.

CI runs a locked Linux Python dependency advisory audit and produces a CycloneDX
inventory. It follows [pip-audit](https://github.com/pypa/pip-audit); this does not
replace scanning actual container images, OS packages or independent review.
Use approved, refreshed advisory data when assembling an offline release.

## Required drills

Before rollout, retain results for migration/grant/restore, account/secret rotation,
collector loss/staleness, local-model failure, uncertain action recovery, gateway
TLS/session expiry, overload and database outage. Preserve unresolved claims on
restore and revoke restored browser sessions. Record owners, approved maintenance
windows, expected detection times, alert delivery and escalation outcomes.
