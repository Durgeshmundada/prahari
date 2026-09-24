# Linux deployment scaffold

This directory is a production-shaped **local deployment scaffold**, not a claim
that PRAHARI is production-ready. The checked-in API can now use PostgreSQL,
migrations, and API-key RBAC, but its default model remains a replay-backed,
heuristic baseline. Live telemetry, an evaluated model, TLS termination, and a
reviewed network-device action adapter still require independent verification.

The Compose file starts a hardened PostgreSQL + migration + grants + API +
dashboard shape. It is intentionally configured as a local development/replay
stack by default. Production settings fail closed until the documented gates are
met.

`api-distroless.Dockerfile` is the shell-free API security candidate. It uses a
matching Python 3.13 Debian 13 build/runtime pair pinned by image index digest,
copies only the locked virtual environment, application files and LightGBM's
three native runtime libraries, and runs as numeric UID/GID 10001. Build, verify
and scan it separately before considering a release-manifest change:

```bash
docker build -f deploy/api-distroless.Dockerfile -t prahari-api:distroless-candidate .
bash deploy/scripts/verify-api-image.sh prahari-api:distroless-candidate
sudo bash deploy/scripts/scan-local-image.sh \
  /home/hp/.local/share/prahari-owned-pilot \
  prahari-api:distroless-candidate candidate-api-distroless
```

The verification runs without network or application secrets, checks native/API
imports and migration CLI startup, enforces the numeric user and read-only root,
and rejects an executable `/bin/sh`. It does not promote the image, approve a
model or replace integration/browser/security acceptance. CI now validates,
builds and executes this candidate on every image-build run in addition to the
current release recipe. The 2026-09-20 offline Trivy 0.74.0 scan produced a
75-component CycloneDX SBOM and found 21 high-severity Debian runtime advisories,
all without a fixed version in that database. This improves the earlier API
candidate's 44 high findings but does not pass a zero-high release gate.

## Prerequisites

Use a supported, patched Linux host or VM (x86_64 recommended) with:

- a rootful Docker Engine running in Linux-container mode and Docker Compose
  v2 (rootless Docker and user-namespace remapping are not validated here);
- Bash, `curl`, and enough local disk to hold preloaded images and artifacts;
- at least 2 vCPU and 4 GiB RAM for the API-only local stack; and
- host firewall rules that expose the API only to the intended operators.

For the Containerlab/FRR network lab, use the separate requirements in
[`../network-lab/README.md`](../network-lab/README.md). Do not treat a Windows
workstation or Docker Desktop as proof that the privileged Linux lab works.

Docker access is privileged access to the host. Limit Docker group membership,
keep the API bound to loopback unless a reverse proxy/firewall policy has been
reviewed, and never mount the Docker socket into application containers.

## Secret files and database roles

Do not place database passwords or database URLs in `deploy/.env`. Compose
mounts one Linux-host secret file at a time, so each service receives only what
it needs:

| Identity / service | Access |
|---|---|
| PostgreSQL bootstrap role | Creates the database roles during first volume initialization; used again only by the short-lived grant job. |
| Migrator role / `migrate` | Owns the database and schema; receives only the migrator database-URL secret. |
| Runtime app role / `api` | Receives app database-URL and API-key JSON secrets, plus browser accounts in production. It cannot alter schema. |
| `audit_events` | The runtime role receives `SELECT, INSERT` only: no `UPDATE` or `DELETE`. |
| `alembic_version` | The runtime role receives `SELECT` only so startup can verify the migration head. |
| Telemetry, probes, events, incident state | The runtime role receives only `SELECT, INSERT, UPDATE`. |
| Closure cases and login counters | The runtime role receives `SELECT, INSERT, UPDATE`. |
| Browser sessions | The runtime role also receives `DELETE` for expiry and revocation. |

The role-creation script runs only when PostgreSQL creates a **new** data
volume. The subsequent grant job runs after Alembic successfully creates the
tables, then the API may start. Re-run the grant job after every future schema
migration so new tables receive deliberate permissions; a missing grant fails
closed rather than giving the app broad default privileges.

Set up local secret files only on the authorized Linux host. The checked-in
templates are inert and must never be deployed:

```bash
cp deploy/.env.example deploy/.env
install -d -m 700 deploy/secrets

# Do this only for files that do not already exist. The PostgreSQL Alpine image
# reads its initialization secrets as UID 70; API/migrate run as UID 10001.
for name in \
  postgres_bootstrap_password \
  prahari_migrator_password \
  prahari_app_password; do
  test ! -e "deploy/secrets/${name}" || {
    printf 'Refusing to overwrite existing secret: %s\n' "$name" >&2
    exit 1
  }
  sudo install -o 70 -g 70 -m 600 \
    "deploy/secrets/${name}.example" "deploy/secrets/${name}"
done

for name in \
  prahari_migrator_database_url \
  prahari_app_database_url \
  prahari_api_keys_json; do
  test ! -e "deploy/secrets/${name}" || {
    printf 'Refusing to overwrite existing secret: %s\n' "$name" >&2
    exit 1
  }
  sudo install -o 10001 -g 10001 -m 600 \
    "deploy/secrets/${name}.example" "deploy/secrets/${name}"
done
```

Replace every copied value through the approved secret mechanism. URL files use
the internal hostname `postgres` and a URL-encoded password, for example
`postgresql+psycopg://prahari_app:URL_ENCODED_PASSWORD@postgres:5432/prahari`.
Do not type real values into shell history or use a raw password in a Compose
environment variable. Put the API-token map in the dedicated one-line JSON
secret file, never in `deploy/.env`; its expected shape is documented in
[`secrets/README.md`](secrets/README.md). Then change each `*_FILE` value in
`deploy/.env` from the checked-in `*.example` template to its corresponding
real file without the `.example` suffix. Do not use `chmod 600` alone: local
Compose file secrets preserve host numeric ownership. The required UID mapping,
rootful-Docker assumption, and a reference to the Docker Compose limitation are
documented in [`secrets/README.md`](secrets/README.md).

## External network profile and approved knowledge

Schema head is `0005_case_search`. Run the migration job before the API and
refresh runtime grants to include `action_operations`. The new migration backfills
indexed closure-case outcome, incident type, entity and network-profile metadata
from retained JSON snapshots; it does not rewrite those snapshots. Backups and
restore count checks include the action table; unresolved incident claims must
survive restoration.
Preserve a database backup before upgrading an existing development database.
Automatic schema creation deliberately refuses older incident tables instead of
silently altering them or failing later during a request.

The `0004` upgrade refuses databases with older `action_apply_requested` or
`rollback_requested` audit events: these events have no operation correlation ID,
so matching them to a later result cannot prove which command completed. Preserve
that history and obtain an approved migration/import and independent route
reconciliation procedure. Do not delete audit events or stamp past this guard.
This workspace's development backup contained zero legacy device intents.

For an interrupted action, stop all API instances that can dispatch and stop the
device agent. Restart an API instance with `PRAHARI_ACTION_ADAPTER_ENABLED=false`
and retain the original database and authenticated reviewer access. Independently
inspect the route through the approved read-only device procedure, retain the
inspection artifact and its SHA-256, and use **Action records → Record verified
state** with a reviewer different from the requester. The API refuses local
reconciliation while its adapter is enabled. The reviewer attests that other
dispatchers are stopped; this does not implement automated vendor fencing.
Re-enable a verified adapter only after the operational review. The checked-in
production overlay continues to prohibit the controlled-lab adapter entirely.

Supply `PRAHARI_NETWORK_PROFILE_FILE` and `PRAHARI_KNOWLEDGE_DIR` in the real
production environment file. Compose mounts them read-only as
`PRAHARI_NETWORK_PROFILE_PATH` and `PRAHARI_KNOWLEDGE_ROOT`. Missing host paths
are rejected instead of being created. Files/directories must be readable by
runtime UID 10001. The checked-in profile is an example scoped `controlled_lab`
and intentionally fails real production validation.

Build your reviewed profile from
[`network-profile.lab.example.json`](../config/network-profile.lab.example.json).
Replace the sites, devices, interfaces, paths, monitored/probe entities, versions,
and network policies with actual target configuration. Select one primary path
and monitored entity. The current prediction schema requires loss/jitter network
evidence and chat/file-upload application probes with 30/60/120-second horizons;
new incident classes or evidence schemas require a separate implementation and
validation. All references are checked at startup. External profiles currently
permit monitoring-only playbooks with `action_adapter: null`.

Add `approved_runbooks` entries for independently reviewed documents relative
to the knowledge directory:

```json
{
  "path": "network/monitoring.md",
  "sha256": "<SHA-256 of the exact UTF-8 file bytes>",
  "reviewer_id": "<accountable reviewer>",
  "approval_reference": "<approved document/change record>"
}
```

Only these documents enter retrieval. Traversal, symlink documents, missing files,
and checksum mismatches fail validation. Review metadata records an approval
reference; it is not a digital signature or independent verification of content.
The model selects verified statement IDs; arbitrary generated claims are rejected.
Catalog enforcement does not independently verify procedural content or reviewer authority.
Set the playbook's `source` to one of these approved relative document paths.

Validate the profile and obtain its canonical checksum:

```bash
uv run python scripts/validate_network_profile.py /approved/network-profile.json
```

The canonical checksum includes validated default values and is independent of
JSON formatting. Bind it in the predictive-v2 independent approval record as
`network_profile_sha256`; production approvals also require production scope,
external validation evidence and a change ticket. Model window/sampling/sample
requirements must match the profile. Any topology, policy or approved-catalog
change requires model applicability review and updated approval. Merely changing
the scope label to `production` does not establish real target validation.

Run both production artifact and Compose validation before starting a release.
Backup manifests include the profile file checksum and reject profile drift at
restore. Preserve the approved configuration, knowledge, model artifacts and
approval records with the release; the database backup is not a copy of these
external files.

## Image and artifact policy

`compose.yaml` uses `pull_policy: never`. Obtain, scan, approve, pin by digest,
and preload images before use. The included [Dockerfile](Dockerfile),
[dashboard.Dockerfile](dashboard.Dockerfile), and
[postgres.Dockerfile](postgres.Dockerfile) expect the repository root as their
build context. The checked-in root `.dockerignore` is an allowlist that
excludes secrets, recordings, local artefacts, and `node_modules`; review it
whenever either image gains a new input.

For offline use, preload the PRAHARI, dashboard, and PostgreSQL images, locked
Python wheels, model artifacts, and documentation through an approved process. Record image
digests, package checksums, licenses, vulnerability scan results, and approval
references in the release record.

## Validate without changing Docker state

On Linux, validate the Compose model with the supplied template settings:

```bash
bash deploy/scripts/validate-compose.sh
```

This invokes `docker compose config` only. It does not build, pull, start, stop,
or remove anything; the template result is not deployable. After provisioning
real secret files, validate their paths, permissions, role names, and URL shape
without printing their contents:

```bash
sudo bash deploy/scripts/validate-compose.sh deploy/.env
```

`PRAHARI_UVICORN_LOG_LEVEL` is a lowercase Uvicorn CLI value (`critical`,
`error`, `warning`, `info`, `debug`, or `trace`). `PRAHARI_LOG_LEVEL` is the
application setting and is normalized independently.

## Start the local stack

After an operator has supplied an approved, prebuilt image:

```bash
# Finish the secret-file setup above, then set approved image digests and retain
# loopback binding in deploy/.env. Real secret files are intentionally owned by
# the non-root container identities, so invoke the local Compose scaffold with
# sudo on the supported rootful Linux Engine.
sudo docker compose \
  --project-directory deploy \
  --env-file deploy/.env \
  -f deploy/compose.yaml up -d
curl --fail http://127.0.0.1:8080/health
```

The `migrate` service applies `alembic upgrade head`; `grant-app-role` then
applies exact runtime table privileges before the API starts. The default health
endpoint reports `fixture_only: true`; that is not evidence of live network
monitoring. The production overlay enables local browser sessions backed by the
API and database. Configure the accounts and HTTPS origin below before starting
that overlay. The static dashboard contains no API keys.

## Local browser accounts and HTTPS

Run account provisioning from an installed PRAHARI environment on the Linux
deployment host. The secret directory must already exist. The following commands
prompt for passphrases; passwords are not command arguments:

```bash
python scripts/provision_browser_user.py --file deploy/secrets/browser-users.json \
  --username operator --actor-id operator-01 --role operator
python scripts/provision_browser_user.py --file deploy/secrets/browser-users.json \
  --username reviewer --actor-id reviewer-01 --role reviewer
sudo chown 10001:10001 deploy/secrets/browser-users.json
sudo chmod 600 deploy/secrets/browser-users.json
```

Use distinct people and identities for operation and independent review. Set
`PRAHARI_BROWSER_USERS_JSON_FILE=./secrets/browser-users.json` and
`PRAHARI_BROWSER_ORIGIN=https://your-approved-hostname` in the production env file.
The checked-in empty account template is deliberately not deployable. Apply all
migrations through `0005_case_search` and rerun the runtime grant service.
Deploy the API and lab action agent together: action requests now include a
required prediction ID, and dry-run authorization is bound to that exact ID.

Adapt `deploy/nginx/tls-gateway.example.conf` with the actual hostname and managed
certificate paths. It terminates HTTPS and forwards to the loopback dashboard
port. Validate the gateway configuration and certificate on the target host
before routing users to it. The example does not provision certificates.

To rotate a password, rerun provisioning with `--replace-user`; it preserves
other accounts. Account-file changes revoke affected sessions when read by the
API. Provisioning uses atomic replacement, so Docker file bind mounts can retain
the old inode: recreate the API container after replacement to load the new file.
Do not rely on an ordinary process restart to refresh a file bind mount.
After deployment or rotation, verify old sessions are rejected and new sign-in
works. Back up the account secret through the managed secret process separately
from the database archive. Restore scripts clear restored browser sessions after
verifying archive contents, requiring users to sign in again.

Login counters and sessions live in PostgreSQL. The runtime database role has
only the required table privileges, including session deletion for revocation;
audit entries remain append-only for that role. A dedicated CI job exercises
migrations, concurrent login counters, competing case decisions, session limits,
and revocation against PostgreSQL using temporary isolated schemas.

Use the following command to stop containers while retaining the database
volume:

```bash
sudo docker compose --project-directory deploy --env-file deploy/.env -f deploy/compose.yaml down
```

Do not add `-v` unless the exact volume and recovery consequences have been
reviewed. Role creation does not rerun for an existing volume: password or role
rotation requires an approved PostgreSQL procedure and a backup/restore plan,
not deletion of the volume.

## Release gates still required

Production artifact preflight now requires `PRAHARI_RELEASE_ACCEPTANCE_PATH` and
`PRAHARI_RELEASE_EVIDENCE_ROOT`. The pending template is not an acceptance record.
Use [OPERATIONS.md](OPERATIONS.md) for exact release identity binding, integrity
checks, metrics, retention and operating drills. API/DB/AI networks stay internal;
only the loopback gateway joins `web_ingress`, with outbound masquerading disabled.
This fixes actual host-port publishing; firewall/egress acceptance remains required.

For the first measured deployment use the separate
[owned pilot](../docs/owned-pilot.md). It keeps experimental monitoring explicit
and does not satisfy the production-model or independent-approval gates.

Before calling any deployment production-ready, require at minimum:

1. a threat model, authenticated authorization model, audit retention policy,
   secret management, TLS/reverse-proxy configuration, and vulnerability scans;
2. database migrations, backup/restore drills, resource limits, logs/metrics,
   alerting, and an operational runbook;
3. integration tests against live telemetry and a controlled Linux lab;
4. load, failure, recovery, and security testing in a production-like
   environment; and
5. named owners approving a rollback plan and change-control process.
