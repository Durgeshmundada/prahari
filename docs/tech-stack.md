# Technology stack

## Selection principle

The MVP uses the smallest local, reproducible components that can support a real demo. Do not add Kafka, a graph neural network, or a large model merely because they appear in an architecture diagram.

## Selected MVP stack

| Area | MVP choice | Why |
|---|---|---|
| Linux host | Ubuntu/Debian VM or bare metal | Required for realistic Containerlab/MPLS support and local services. |
| Network lab | Containerlab plus FRRouting | Reproducible topology-as-code and open routing stack. |
| Tunnel/path endpoints | Linux or VyOS endpoints | Lets the team expose and control primary/backup paths. |
| Fault injection | Explicit tc netem scripts | Reproducible gradual loss/jitter/delay and automatic labels. |
| Application traffic | Local web app plus file/video probes | Gives the audience visible user impact. |
| Collection | Telegraf or a small Python collector; syslog receiver | Start with one working collection path, then add adapters. |
| Metrics store | Prometheus or TimescaleDB | Local numeric time-series queries and dashboards. |
| Structured store | PostgreSQL or SQLite for MVP | Inventory, labels, predictions, feedback, audit. |
| Analytics | Python, pandas, scikit-learn, LightGBM | Good for windowed features, calibration, and explainability. |
| Topology | NetworkX plus versioned topology YAML/JSON | Simple, explainable correlation before a GNN. |
| Backend | FastAPI | Typed local APIs and response validation. |
| Dashboard | React with a topology graph library | Shows incident cards, evidence, path, and approval states. |
| Local RAG | FAISS plus local embeddings for MVP | No external vector service; easy air-gapped demonstration. |
| Local LLM | Ollama or llama.cpp with a compatible small open-weight model | Local structured explanation only. |
| Validation | Playbook-specific checks; NETCONF/YANG only when supported | Keeps actions safe and explicit. |
| Security/audit | Local firewall rules, RBAC, append-only audit records | Enforces and proves local-only operation. |

## Models

| Stage | Model/logic | Reason |
|---|---|---|
| MVP trend alert | Rolling slope, EWMA, robust z-score, threshold-crossing estimate | Works with limited lab data and is easy to explain. |
| MVP risk/type | Calibrated LightGBM classifier | Handles mixed features and modest datasets. |
| MVP location | Entity IDs plus topology heuristic | More honest than training a location model without enough data. |
| MVP ETA | Horizon-risk probabilities plus threshold-crossing estimate | Reports a useful range instead of a false exact timestamp. |
| Later sequence model | Small multi-task GRU | Only after enough independent, reviewed fault runs exist. |
| Later graph model | GraphSAGE or similar | Only after topology diversity and labels justify it. |

See [ml-model.md](ml-model.md) for the full model contract.

## Do not make these dependencies mandatory for the MVP

- a cloud LLM, hosted embeddings, or hosted monitoring;
- a vendor SD-WAN controller that the team cannot lawfully run;
- GPU-only tooling;
- a message bus before a direct local collector works;
- LSTM, GRU, or GNN training before a simple baseline is measured;
- autonomous router configuration.

## Hardware baseline

Exact requirements depend on node count and local model size. For a small demo, plan for:

- a Linux-capable host with virtualization/container support;
- enough RAM for the network containers, metrics store, and local model;
- local disk space for raw telemetry and experiment artifacts;
- an optional GPU only if the selected local LLM needs it.

Measure actual memory use before presentation. If the local LLM is too heavy, use a smaller quantized model; do not move inference to a cloud service.

