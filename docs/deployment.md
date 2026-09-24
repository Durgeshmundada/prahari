# Local deployment

## Deployment goal

Run the lab, telemetry, model, knowledge base, dashboard, and any local model on infrastructure controlled by the project team. No component should require a cloud LLM, hosted vector database, hosted telemetry service, or runtime package download.

## Deployment shape

~~~text
Linux host or VM
├── network-lab containers/namespaces
├── local management network
├── collector and syslog receiver
├── metrics and structured stores
├── feature/prediction services
├── local retrieval/index and optional local LLM
├── FastAPI backend and dashboard
└── experiment/artifact storage
~~~

For a small demo, these may run on one capable Linux host. Separate them only when resource isolation or realism requires it.

## Startup order

1. Verify host networking, kernel capabilities, disk space, and local artifacts.
2. Start the topology and verify primary, backup, and management reachability.
3. Start inventory/configuration version service or load the static versioned files.
4. Start telemetry collectors and confirm record freshness.
5. Start metrics/structured stores.
6. Start feature and prediction services with a registered model.
7. Start local retrieval and optional local LLM.
8. Start backend and dashboard.
9. Run a healthy application traffic check.
10. Run a no-fault smoke test, then the explicit demo fault only when ready.

## Offline artifact import

Before disconnecting the environment:

- obtain container images, packages, models, and documentation through an approved process;
- record source, version, checksum, licence, and approval;
- scan/verify artifacts according to local policy;
- copy them to controlled local storage; and
- test installation with network egress disabled.

Do not rely on downloading dependencies during a presentation.

## Configuration inputs

Keep these versioned and visible in the UI/audit record:

| Artifact | Examples |
|---|---|
| Topology | Node/link/path IDs, management/data addressing, capacity, roles |
| Inventory | Site names, device mappings, owner, primary/backup relationship |
| Collection | Credentials reference, source cadence, metric mapping |
| Model | Feature version, model version, calibration, alert thresholds |
| RAG corpus | Runbook/case IDs, reviewer and approval state |
| Action policy | Required approver, dry-run command/check, rollback procedure |

## Backups and recovery

Before a demo, export or snapshot:

- lab topology/configuration;
- model and dataset manifest;
- local RAG index and documents;
- healthy baseline recording;
- fault manifest;
- dashboard configuration; and
- current incident/audit records.

The reset procedure must be tested before the demo, not written for the first time during it.

