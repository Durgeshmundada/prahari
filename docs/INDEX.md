# PRAHARI documentation index

This directory is the canonical implementation specification for PRAHARI. Read the documents in the following order when starting the build.

| Order | Document | Why it matters |
|---:|---|---|
| 1 | [architecture.md](architecture.md) | Defines the components, trust boundaries, and responsibilities. |
| 2 | [network-lab.md](network-lab.md) | Defines the SD-WAN/MPLS-first lab and the real application traffic used in the demo. |
| 3 | [telemetry-and-data-contract.md](telemetry-and-data-contract.md) | Defines how router health becomes reliable model input. |
| 4 | [ml-model.md](ml-model.md) | Defines what the predictor receives, which models are used, and how it estimates risk, location, and time. |
| 5 | [data-and-labelling.md](data-and-labelling.md) | Defines how to build an honest dataset, labels, splits, and held-out demo scenario. |
| 6 | [feedback-and-incident-memory.md](feedback-and-incident-memory.md) | Defines safe learning from network caretaker feedback and reuse of approved solutions. |
| 7 | [incident-taxonomy.md](incident-taxonomy.md) | Defines incident classes, severity, and evidence requirements. |
| 8 | [rag-pipeline.md](rag-pipeline.md) | Defines local retrieval, citations, response schema, and safety gates. |
| 9 | [workflow.md](workflow.md) | Defines the end-to-end operational flow. |
| 10 | [api-and-ui-contract.md](api-and-ui-contract.md) | Defines dashboard states and backend response contracts. |
| 11 | [demo-runbook.md](demo-runbook.md) | Defines the presenter script and proof required for a real demo. |
| 12 | [evaluation-plan.md](evaluation-plan.md) | Defines metrics and the claims the project may make. |
| 13 | [implementation-plan.md](implementation-plan.md) | Defines milestones, dependencies, and definitions of done. |
| 14 | [tech-stack.md](tech-stack.md) | Records the selected MVP technologies and later upgrades. |
| 15 | [deployment.md](deployment.md) | Defines local/offline deployment and startup expectations. |
| 16 | [security-and-safety.md](security-and-safety.md) | Defines air-gap, approval, audit, and remediation safety rules. |
| 17 | [mvp-research-and-decisions.md](mvp-research-and-decisions.md) | Records current research findings, assumptions, and active build decisions. |
| 18 | [local-development.md](local-development.md) | Explains how to run the replay slice and which components require Linux. |
| 19 | [production-readiness.md](production-readiness.md) | Separates implemented safeguards from the evidence still required for release. |
| 20 | [traffic-demo.md](traffic-demo.md) | Runs the bounded Branch-to-DC chat/file evidence workload. |
| 21 | [production-build-plan.md](production-build-plan.md) | Records the implementation review and remaining acceptance work. |
| 22 | [owned-pilot.md](owned-pilot.md) | Runs separate virtual servers and collects measured network/application evidence. |
| 23 | [current-project-status.md](current-project-status.md) | Gives the current usable scope, verified checks, and remaining release gates. |

The [runbooks](runbooks) directory is the trusted operational corpus for local RAG. It should contain reviewed procedures, not generated commands.

## Key terms

- **Prediction:** a structured network assessment. Heuristic impact scores and the experimental present-degradation classifier are distinct from an approved calibrated forecasting bundle.
- **Incident memory:** a reviewed record of a previous incident, resolution, evidence, and outcome.
- **RAG:** approved local retrieval followed by model selection of evidence-bound statement IDs; the server verifies support and renders the explanation.
- **Time-to-impact:** a range until a defined application or service threshold is likely to be breached. It is not a promise of an exact failure time.
- **Caretaker:** a network operator or engineer who investigates, resolves, and reviews an incident.
