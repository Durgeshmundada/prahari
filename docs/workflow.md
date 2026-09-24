# Operational workflow

## Simple workflow

1. Users send chat messages, images/files, or video through the primary SD-WAN/MPLS lab path.
2. Routers, tunnels, application probes, and routing logs send health observations to the local collector.
3. PRAHARI normalizes those observations and creates rolling features for each interface, tunnel, device, and path.
4. The prediction stack looks for an abnormal trend and estimates the chance of impact within each time horizon.
5. The topology correlator maps the highest-risk entity to its site, path, neighboring entities, and affected application scope.
6. PRAHARI retrieves relevant local runbooks and reviewed prior cases.
7. The dashboard presents a cited incident brief and a safe proposed action.
8. The operator verifies the evidence, runs a dry-run/check, approves a controlled change if appropriate, and validates recovery.
9. The operator closes the case with what happened, what worked, what failed, and the observed outcome.
10. A reviewer approves the closure record. The approved case is immediately available to local RAG; its verified labels enter the next offline training cycle.

## End-to-end sequence

~~~mermaid
sequenceDiagram
    participant App as Client and app server
    participant Net as SD-WAN/MPLS lab
    participant Col as Local collector
    participant ML as Predictor and topology correlator
    participant RAG as Local RAG and incident memory
    participant Op as NOC operator

    App->>Net: Chat, file, and video traffic
    Net->>Col: Metrics, events, route/tunnel health
    App->>Col: Application-health probe metrics
    Col->>ML: Normalized rolling feature window
    ML->>ML: Estimate risk, location, type, ETA range
    alt risk crosses alert policy
        ML->>RAG: Structured incident candidate and evidence IDs
        RAG-->>Op: Cited brief and approved playbook candidates
        Op->>Op: Review evidence and data quality
        Op->>Net: Approve dry-run or controlled mitigation
        Net-->>Op: Validation and recovery evidence
        Op->>RAG: Reviewed closure feedback
    else low risk or poor data quality
        ML-->>Op: Trend/data-quality view only
    end
~~~

## Incident states

~~~mermaid
stateDiagram-v2
    [*] --> Healthy
    Healthy --> Watch: trend/anomaly detected
    Watch --> Predicted: calibrated risk passes alert policy
    Watch --> Healthy: trend recovers
    Predicted --> Reviewed: operator opens evidence
    Reviewed --> Mitigating: approved runbook/dry-run
    Reviewed --> Monitoring: action deferred
    Mitigating --> Validating: action completed
    Validating --> Resolved: metrics and application recover
    Validating --> Escalated: validation fails
    Monitoring --> Resolved: risk recovers
    Resolved --> ReviewedFeedback: caretaker closes case
    Escalated --> ReviewedFeedback: caretaker closes case
    ReviewedFeedback --> Healthy: reviewer accepts record
~~~

## Alert policy

An alert requires all of the following:

- sufficient telemetry quality;
- a calibrated risk above the configured threshold;
- a likely impact before the selected horizon;
- an identifiable entity/path and topology version;
- evidence that impact has not already started, if the alert is presented as predictive.

The policy should suppress duplicate alerts and downgrade confidence when telemetry is incomplete.

## What the operator sees

Every incident card should answer:

| Question | Required answer |
|---|---|
| What may happen? | Likely incident type and impact description. |
| Where? | Ranked device, interface/tunnel/path, site, and affected scope. |
| When? | Time-to-impact range and horizon probabilities. |
| Why? | Trend values, events, topology evidence, and data quality. |
| What can we do? | Approved runbook/case steps, not raw generated commands. |
| What happened after action? | Validation metrics and closure outcome. |

