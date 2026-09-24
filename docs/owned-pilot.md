# Owned server pilot

The first acceptance environment runs on our own local WSL2 Ubuntu server.
These are isolated virtual servers, not rented hardware or a customer deployment.
The separate `prahari-owned` namespace does not change the original `prahari` lab.

## Deployment

| Component | Current role |
|---|---|
| Six FRR routers | Primary and higher-cost backup routed OSPF paths |
| Branch client and application server | Actual ICMP, chat and verified binary-file probes |
| PostgreSQL | Durable telemetry, browser sessions, incident evidence and audits |
| API | Live measurements, explicit heuristic assessments, guarded workflows |
| Dashboard | Local sign-in, measured application results and incident investigation |
| Ollama | Own cached model volume, read-only weights, non-root local advisory generation |

The management subnet is the owned preset `172.30.254.0/24`. Router data-plane
addresses reuse the logical `lab-v1` configuration inside separate container
network namespaces. This is controlled lab evidence and does not prove model
generalisation. Startup checks subnet conflicts, exact pinned images, container
ownership, frozen runtime/configuration hashes and data links.

The dashboard is at **http://127.0.0.1:8520**. Only the gateway publishes a
loopback port; API, PostgreSQL and AI ports are not published. Random passwords,
separate collector/viewer machine tokens and separate database-role credentials
were generated under the private Linux state directory:

```text
/home/hp/.local/share/prahari-owned-pilot
```

Sign in as `operator` or `reviewer` using the corresponding locally saved password:

```powershell
wsl.exe -d Ubuntu-24.04 -- cat /home/hp/.local/share/prahari-owned-pilot/credentials.txt
```

Passwords are not committed or printed by provisioning. Browser credentials are
scrypt hashes; the clear provisioning receipt is owner-readable only. Production
must use its reviewed HTTPS deployment instead of this localhost development URL.

## Runtime and recovery

`scripts/prepare_owned_pilot.py` creates a new state directory and refuses to
overwrite existing credentials. All supplied server images must be exact
preloaded digests. Routing/probe images are the explicitly pinned owned-lab preset.
The prepared `runtime` copy freezes collector/controller/configuration sources;
workspace changes cannot silently alter a running experiment. Source hashes and
image identities remain in `pilot-manifest.json`.

`network-lab/scripts/start-owned-pilot.sh STATE` validates that manifest, creates
only owned resources, runs migrations and exact application grants, and starts
the actual application receiver. After Docker/WSL loses veth links it recreates
only owned routing/probe nodes. PostgreSQL and model volumes are preserved.
No existing customer lab containers, services or recovery timers are stopped.

The root-owned Linux service `prahari-owned-pilot.service` runs the foreground
supervisor and restarts it after a Linux-side failure. The Windows task
`PRAHARI-Owned-Pilot` wakes and holds the WSL environment from an interactive
logon; the Linux service restores supervision whenever WSL starts again. The
task uses the current interactive user and hidden windows; it does not create a
system-wide elevated Windows account. Privileged routing operations occur only
inside the explicitly owned WSL lab.

`PRAHARI-Owned-Pilot-Capacity` runs every five minutes. It resolves the selected
distribution's current registry-backed VHD location, checks free space on that
Windows drive and on the Linux state filesystem, and verifies that `/` is still
read-write. The default floor is 20 GiB for each layer. Its latest atomic report
is `artifacts/owned-pilot-host/capacity.json`; an unhealthy check exits with code
2 so Task Scheduler records a failure. The report binds the checker source by
SHA-256. The supervisor also refuses a new start
when the VHD drive is below its configured floor. This is local detection
evidence; route the task failure into the accepted operations alerting system on
an unattended server.

`PRAHARI-Owned-Pilot-Watchdog` independently checks the long-running keeper task
every five minutes. It validates the registered action against the reviewed host
runner before starting a stopped keeper and writes an atomic, checker-hash-bound
report to `artifacts/owned-pilot-host/supervisor-watchdog.json`. This closes the
local gap where a keeper terminated after logon and WSL later stopped, while still
requiring external alert delivery for an unattended deployment.

```powershell
Get-ScheduledTask -TaskName PRAHARI-Owned-Pilot
Get-ScheduledTask -TaskName PRAHARI-Owned-Pilot-Capacity
Get-ScheduledTask -TaskName PRAHARI-Owned-Pilot-Watchdog
Start-ScheduledTask -TaskName PRAHARI-Owned-Pilot
Stop-ScheduledTask -TaskName PRAHARI-Owned-Pilot
```

Stopping supervision does not delete databases, evidence or model volumes.
The foreground helper and systemd service are available for manual startup.
Operational logs are in
`artifacts/owned-pilot-host`; measured evidence and private credentials stay in
the Linux state directory. An interactive logon is required for this local pilot;
an unattended Linux host is the next infrastructure option.

On 2026-09-20 the original C:-backed WSL VHD exhausted its host drive during a
large candidate-image pull. Linux logged block I/O errors and remounted ext4
read-only. The distro was stopped and moved with `wsl --manage` to
`D:\WSL\Ubuntu-24.04`; ext4 returned read-write with no new block errors, all
four services became healthy, and the campaign resumed from its last sealed
checkpoint. The two-layer capacity monitor and startup floor now detect a repeat
before collection starts or during operation. Delivery of that task failure to
an accountable operator and an independent filesystem/restore drill remain
required.

## Measurement and model work

The supervisor collects joined actual network/application observations. A shared
measurement lock prevents normal probes from being falsely labelled during
controlled impairment. Forecasting experiments use the frozen controller:

```bash
state=/home/hp/.local/share/prahari-owned-pilot
bash "$state/runtime/network-lab/scripts/run-predictive-experiment.sh" \
  --lab-name prahari-owned --state-dir "$state" \
  --scenario gradual_loss --profile a --experiment-seed 20260916 \
  --traffic-profile standard --confirm RUN-PREDICTIVE-V2
```

Only the explicit controlled namespaces `prahari` and `prahari-owned` are
accepted. Physical controller identity is preserved in observations and sealed
manifests; fault schedules must target the matching namespace. Unknown routers,
interfaces and arbitrary production targets remain rejected. The action API
adapter is disabled; a verified cleared netem fault does not imply an API-driven
mitigation, approved model or customer recovery acceptance.

An interrupted capture stays unsealed and is excluded from training. Collection
must produce at least twelve independently measured impacted and twelve normal
runs with traffic/fault diversity, then pass whole-run dataset integrity, held-out
calibration, false-alert and lead-time checks. No model approval, case approval
or customer acceptance is automatically fabricated.

The bounded campaign controller is `scripts/run_owned_model_campaign.py`. It
stages its own immutable runtime, preserves the pilot release hash, varies seed,
traffic and fault profile, and refuses a release change during collection. Each
controller has a 30-minute deadline and cancellation terminates only its owned
process group. Additional delay scaling is explicit, bounded to 100–250 percent,
restricted to the owned lab, and recorded in the campaign report and actual fault
schedule. It changes experimental conditions, never measured impact labels.

The current report is
`model-campaigns/20260914T172316Z-3d77aacf/summary.json` under the private state
directory. Collection completed with 15 measured impacted and 12 normal runs.
Impacted traffic coverage was light/standard/heavy 7/3/5 and fault-profile
coverage was a/b/c 5/6/4; normal traffic coverage was 4/4/4. Sixty controller
attempts failed while the controlled interface was unavailable, three failed
measurement-integrity checks, and eight interrupted attempts remain excluded.
These failures and resumptions are retained rather than relabelled.

Campaign resumption rehashes the pilot, frozen source, every selected manifest
and every attempt log before continuing at a new seed/log index. Infrastructure
startup failures do not consume the measurement-failure budget, but an absolute
attempt bound still applies. Repeated restart records are moved into immutable
SHA-256-inventoried audit files before the live summary exceeds its metadata
bound. Exhausting the bounded measurement attempts stops with
`attempts_exhausted`; it does not enter an unbounded systemd restart loop.
When a resumed cohort is short of a traffic-profile quota, the controller now
selects that missing profile directly instead of rotating from its historical
attempt number. Previously sealed runs and their measured labels are unchanged.

`scripts/complete_owned_model_campaign.py` waits for completed collection, verifies
source/evidence hashes, stages only selected sealed runs, then runs the existing
strict exporter and trainer as UID 10001 in the pinned image with no network,
read-only source/inputs and bounded resources. Whole-run train/validation/test/demo
partitions remain separate. Results go into the campaign's `model-build` directory.
The root-owned `prahari-owned-pilot.service` now restores and supervises the lab.
`prahari-owned-model-campaign.service` waits for the controlled interface and
resumes the verified cohort after a WSL restart. The separate
`prahari-owned-model-evaluation.service` waits for collection and builds an
idempotently verified candidate. A failed validation gate is retained as an atomic,
checksum-inventoried rejection with its assessed thresholds and with test/demo
holdout explicitly unevaluated. It is a completed evaluation outcome rather than a
retryable infrastructure failure; no service approves or activates a model.
If export or training is interrupted, the evaluator validates and preserves the
partial `model-build` directory as a checksum-referenced attempt, deletes only
its derived staged input copies, and retries from the original sealed cohort.
An already completed build remains immutable and is returned only while every
inventoried artifact, cohort hash and pilot hash still matches.

The first evaluation completed on 2026-09-20. A measured timing gap exposed an
exporter/validator mismatch: usable-feature exposure could exceed the 15-second
evidence ceiling even while every raw observation gap was valid. The evaluation
exporter now caps exposure at that ceiling and records its own SHA-256 separately
from the unchanged campaign runtime. Eight failed build attempts are preserved
with immutable inventories. The corrected build completed, but its 120-second
technical gate failed: test precision was 0.667, recall 1.000, false alerts 38.81
per eligible negative link-hour, median useful lead time 100.15 seconds, and ETA
coverage 0.500. The required precision and false-alert checks failed. The model
remains unapproved, unscoped and inactive; do not tune against the revealed test
or demo-holdout results. Sanitized reports are under
`artifacts/owned-model-evaluation`.

The v3 campaign completed 18 fresh impacted and 18 fresh normal runs under the
root-protected, restart-resumable service. Validation-only threshold selection
passed its 120-second constraints with 1.0 precision/recall, zero false-alert
episodes and 97.40 seconds median useful lead. The untouched test split retained
1.0 precision/recall and 102.08 seconds median useful lead, but one false-alert
episode over 0.0434 eligible negative link-hours yielded 23.05 alerts/hour against
the 1.0 maximum. Its Brier score was 0.0728 against the 0.25 maximum. The technical
gate therefore failed. The separately reported demo holdout was excluded from the
gate and produced 1.0 precision/recall, zero false-alert episodes and 111.35
seconds median lead. The evaluator hash-inventories its exporter, trainer, cohort
and outputs. The candidate is unapproved, has no deployment scope, is not active,
and must not be tuned against the now-revealed test or demo holdout.

The next campaign is versioned as v4 and uses the uncertainty-aware technical
gate. Its collector binds a 600-sample normal baseline into the resumable campaign
identity and varies duration deterministically. The exporter assigns whole normal
runs until validation, test and demo holdout each have at least three eligible
negative link-hours, while preserving six or more normal training runs and all
traffic profiles. A new prospective 18-runs-per-class cohort started on 2026-09-21
with base seed 2050000; its protected summary is
`model-campaigns/20260921T034421Z-f71ac665/summary.json`. The root-owned v6
collector and v12 evaluator are checksum-separated from v3, and the evaluator
has a bounded 48-hour wait. An import-only check against the frozen campaign
runtime found that evaluator v11 depended on gate constants absent from that
runtime; v12 makes the trainer policy self-contained and passed the same-image
import check before the waiting service was repointed. Collection was not
restarted. The correction and hashes are recorded in
`artifacts/owned-pilot-host/model-v4-evaluator-v12.json`. No v3 holdout is reused,
and neither service can approve or activate the candidate.

On 2026-09-22 the registered Windows keeper, owned-pilot, capacity and watchdog
tasks were re-enabled. The pilot and both model services remain active after the
interactive WSL command exits, and the latest capacity/watchdog reports are
healthy. The audit snapshot had all 18 impacted runs sealed and had begun the
normal-soak phase, with zero normal runs sealed at that instant. It recorded
two failed measurements and an unapproved model. Both failed measurements were
rejected by the causal timestamp contract after the clock moved backward during
a joined probe. WSL reports synchronized NTP, while Windows `w32tm` reports an
unsynchronized Local CMOS Clock. Correct host time synchronization before using
the remaining long-soak timing as acceptance evidence; do not relax the causal
timestamp gate.

```bash
systemctl status prahari-owned-pilot
systemctl status prahari-owned-model-campaign
systemctl status prahari-owned-model-evaluation
journalctl -u prahari-owned-model-campaign --no-pager -n 20
```

## Verified pilot operation

Actual browser checks passed for keyboard sign-in, session retention on reload,
live incident detail, disabled device dispatch, a grounded local copilot brief,
desktop/mobile overflow and logout. Evidence is in
`artifacts/owned-pilot-check/verification.json`, with screenshots and the captured
brief. A separate disposable replay instance also passed actual 60-second idle
expiry, re-login and immediate session revocation after disabling its account;
see `artifacts/owned-pilot-check/session-verification.json`. This checks the
mechanism, not production HTTPS/identity-policy acceptance. Broader accessibility
acceptance remains open.

The PostgreSQL backup helper now exports one repeatable-read snapshot for both
`pg_dump` and manifest row counts. The owned pilot's real archive was restored
into an isolated, network-disabled PostgreSQL 17 instance: all ten table counts
and schema revision matched in six seconds. The source database was not switched.
`artifacts/owned-pilot-check/restore-verification.json` records the archive, helper
and pilot hashes; the database archive stays private on the Linux server. This
checks the corrected backup mechanism, not production failover or an independent
restore acceptance. The owned application-probe token was also rotated and the
receiver restarted; complete account/credential rotation acceptance remains open.

All four deployed images were scanned with checksum-verified Trivy 0.74.0 in a
bounded worker with no Docker socket or application secrets. Reports are in
`artifacts/security-check/owned-containers`. The deployed images have high/critical
advisories, so the scan does not grant release acceptance. Updated API/dashboard
candidates built and passed native imports and isolated browser checks. The API
candidate reports zero critical but 44 high advisories; the dashboard reports no
findings with an Alpine EOL-metadata warning requiring coverage review. A hardened
PostgreSQL 17 candidate upgrades the fixed Alpine crypto/UUID packages, removes
`gosu`, and runs the upstream entrypoint as UID/GID 70. Its current Trivy report
has no findings; it also passed fresh initialization, all six application
PostgreSQL integration tests, and an isolated network-disabled restore with exact
schema and all ten table counts. The same Alpine coverage warning applies.
Candidate reports are under `artifacts/security-check/owned-containers`.
Candidates remain separate from the measured release. No vulnerabilities are
automatically waived and no production/defence approval is inferred from the
scans.

A second API candidate uses the official Python 3.13 Debian 13 distroless runtime,
contains no executable shell or package manager, and runs as UID/GID 10001 with
a read-only root, dropped capabilities and no network during verification. The
offline scan produced a 75-component CycloneDX SBOM and reduced the high findings
from 44 to 21. Those 21 findings are Debian runtime advisories in Python, expat,
ncurses and UUID packages; Trivy reported no fixed version for any of them. The
candidate therefore remains unapproved. CI builds and executes both the current
release recipe and this candidate so the hardened path cannot silently regress.

The official Ollama 0.34.2 amd64 image was also pulled by immutable digest and
scanned as a separate candidate. It still embeds Go 1.26.0 and the same affected
module versions, so it retains 43 high findings and does not improve the release
gate. The measured Ollama image was not replaced.

## Acceptance still required

Keep the model labelled heuristic/experimental until measured forecasting
evaluation and independent approval are complete. Record actual capacity,
backup/restore, credential rotation, session expiry, firewall/egress and security
drills against the own-server release. A local working pilot does not establish
high availability, vendor device fencing or defence accreditation. The structural
[release gate](../deploy/OPERATIONS.md) binds evidence; it cannot verify that a
reviewer is independent or that a submitted report is true.
