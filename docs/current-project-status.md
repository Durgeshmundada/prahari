# Current project status

Status date: 2026-09-22.

## What works now

- The formal Observability Studio dashboard consumes the real PRAHARI API and
  includes a responsive hamburger navigation drawer.
- The two-laptop service transfers real chat and bounded file data, measures
  application delay and throughput, verifies SHA-256 integrity, and discards
  content after verification unless a temporary download is explicitly offered.
- Safe demo scenarios show healthy, gradual-loss, high-jitter, burst-loss and
  combined-fault states without changing Wi-Fi, routing or the college network.
- The owned WSL server runs PostgreSQL, the API, dashboard gateway, controlled
  network lab, collectors and the experimental model campaign under supervision.
- Review, case closure, audit, evidence-integrity, browser-session and safety-gated
  action workflows are implemented. Device dispatch remains disabled in the
  owned pilot.

## Verification completed on 2026-09-22

- Ruff passed; 305 backend tests passed with eight environment-specific skips.
- All six skipped PostgreSQL concurrency tests passed separately against a fresh
  disposable PostgreSQL 17 database.
- Twenty-eight dashboard tests, TypeScript checks and the production build passed.
- npm and locked Python runtime dependency audits reported no known findings.
- A clean database migrated through schema `0005` and passed evidence verification.
- Containerlab validated the nine-node/eight-link topology and preflight passed.
- Compose security invariants and all 32 mutation tests passed.
- Prometheus parsed six alert rules and passed the rule simulations using the
  checksum-verified pinned tool.
- Every safe dashboard fault scenario passed end to end through the loopback API.
  Healthy telemetry is now reported as `NO ACTIVE INCIDENT`, preventing a false
  demonstration claim. Recorded replay was restored after the check.

The machine-readable evidence summary is
[`artifacts/local-acceptance-20260922/verification.json`](../artifacts/local-acceptance-20260922/verification.json).

## Work that cannot be completed locally today

1. **Finish and evaluate model v4.** The prospective campaign is still running.
   At the audit snapshot all 18 impacted runs were sealed and the normal-soak
   phase had begun, with no normal run sealed yet.
   It must finish the full cohort and untouched evaluation before any approval.
2. **Correct host time synchronization.** WSL reports synchronized NTP, but
   Windows reports an unsynchronized Local CMOS Clock. Two joined measurements
   were correctly rejected after non-causal timestamps. Timing evidence is not
   acceptable until the host clock is stable.
3. **Repeat browser acceptance for the redesigned UI.** Unit, contract, TypeScript
   and build checks pass. A fresh real-browser keyboard, responsive, session-expiry
   and error-state run still requires an available browser-control session.
4. **Produce a new presentation video.** The existing edited video is valid for
   the older interface. Record the redesigned interface only after the fresh
   browser acceptance run.
5. **Clear the release security gate.** Existing container scans still contain
   high-severity advisories in the API and Ollama candidates, and an independent
   security review has not occurred.
6. **Integrate a real target environment.** Vendor collectors, dispatch fencing,
   target-device evidence, multiple concurrent incident streams, managed identity,
   HTTPS, firewall acceptance, alert delivery and rollback drills need the actual
   customer or college environment and responsible approvers.

This is a strong final-year demonstration and controlled research pilot. It is
not yet certified production software or defence-accredited equipment. No local
code change can grant those external approvals.
