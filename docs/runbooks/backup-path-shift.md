# Runbook: primary-path degradation and controlled backup-path shift

## Metadata

| Field | Value |
|---|---|
| Playbook ID | `backup-path-shift-v1` |
| Status | Available pending per-incident operator review in the PRAHARI Containerlab only; not approved for production devices |
| Applies to | Verified or likely primary-path/tunnel degradation with a healthy backup path |
| Requires | Operator approval, backup-path checks, rollback plan |
| Does not apply to | Unknown topology, unavailable backup path, unverified scope, or an unapproved device-control method |

## Purpose

Reduce service impact by moving eligible demo traffic away from a degraded primary path to a verified healthy backup path.

## Required evidence

- Primary-path loss, jitter, latency, or interface/tunnel state supports the incident.
- Affected branch/site/path is known from the topology.
- The backup path is reachable and healthy.
- No maintenance/change window invalidates the intended action.
- The operator has reviewed the incident card and evidence.

## Pre-action checks

1. Confirm the incident is not a telemetry-source failure.
2. Confirm the primary and backup path IDs and expected routing/overlay behavior.
3. Confirm application probes can reach the server through the backup path in the lab.
4. Record baseline app and network health.
5. Run the approved dry-run/validation operation for the chosen implementation.
6. Obtain explicit operator approval and record it.

## Controlled action

Perform only the specific reversible traffic-shift/failover action approved for the lab implementation. The action adapter must use a known playbook implementation, not free-form LLM output.

## Validation

After the change:

1. Confirm the selected traffic uses the backup path.
2. Confirm primary-path fault evidence no longer affects the selected traffic.
3. Compare chat latency, upload result, and video/probe values with the recorded baseline.
4. Confirm no unexpected affected scope appears.
5. Record before/after timestamps and metrics.

## Rollback

Return traffic to the documented normal primary-path policy only after:

- the injected fault is removed;
- the primary path is healthy for the agreed stability interval; and
- an operator authorizes the restoration.

## Closure

Record the verified root/location, action, outcome, steps that worked/failed, evidence, and reviewer decision using [the feedback procedure](../feedback-and-incident-memory.md).
