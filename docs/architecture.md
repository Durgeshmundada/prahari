# System architecture

## Purpose

PRAHARI has three separate responsibilities:

1. **Predict** likely service impact from telemetry.
2. **Explain and recommend** using local evidence, approved runbooks, and reviewed past cases.
3. **Protect operations** by requiring human approval and validation before any network change.

The language model is not the prediction engine and is not allowed to invent or execute router commands.

## Canonical architecture

~~~mermaid
flowchart TB
    subgraph DP[Data plane: actual demo traffic]
        UC[Branch user client] --> E1[Branch SD-WAN edge]
        E1 --> PRI[Primary MPLS underlay and overlay path]
        E1 --> BAK[Backup path]
        PRI --> E2[Hub or DC edge]
        BAK --> E2
        E2 --> APP[Local chat, file, and video server]
    end

    subgraph MP[Out-of-band local management plane]
        E1 -. SNMP, gNMI, syslog, flow, probes .-> COL[Telemetry collector]
        E2 -. SNMP, gNMI, syslog, flow, probes .-> COL
        COL --> STORE[(Metrics, events, topology, inventory)]
        STORE --> FEAT[Feature and data-quality service]
        FEAT --> PRED[Predictor: trend rules and LightGBM]
        PRED --> CORR[Topology correlator and incident classifier]
        CORR --> MEM[(Reviewed incident memory)]
        CORR --> RAG[Local retrieval and offline LLM]
        KB[(Runbooks, configs, inventory)] --> RAG
        MEM --> RAG
        CORR --> API[FastAPI service]
        RAG --> API
        API --> UI[NOC dashboard]
        UI --> FB[Operator feedback and closure review]
        FB --> MEM
        FB --> DS[(Versioned training dataset)]
    end

    UI -. approved, dry-run action only .-> E1
    UI -. approved, dry-run action only .-> E2
~~~

## Planes and boundaries

| Plane | Contains | Important rule |
|---|---|---|
| Data plane | The user traffic path for chat, files, images, and video | The demo must show traffic genuinely traversing the lab paths. |
| Management plane | Collectors, dashboard, APIs, telemetry store, model, and local RAG | Keep it local and out of band so observation survives a data-path outage. |
| Control plane | BGP, OSPF, MPLS labels, tunnel/path selection, QoS policies | Monitor it, but do not alter it without operator approval. |
| Knowledge plane | Runbooks, inventory, configuration baselines, incident records | Only reviewed, versioned local documents can be retrieved. |

## Component responsibilities

| Component | Input | Output | Must not do |
|---|---|---|---|
| Network lab | Application traffic, routing, tunnel configuration | Packets, device metrics, events | Pretend that synthetic UI traffic is real data-plane proof. |
| Collector | Router/tunnel telemetry | Normalized records | Silently discard bad timestamps or source identity. |
| Feature service | Normalized records and topology | Windowed features, data-quality flags | Train a model during a live incident. |
| Predictor | Features for each entity/path | Risk by time horizon and incident-type probabilities | Generate remediation steps. |
| Topology correlator | Entity scores and graph | Ranked root location and affected scope | State certainty where evidence is ambiguous. |
| Incident memory | Reviewed closure records | Similar previous cases | Treat similarity as proof of a safe action. |
| RAG/copilot | Structured prediction and retrieved documents | Cited incident brief | Make uncited claims or autonomous changes. |
| Dashboard | API responses | Operator review, approval, feedback | Hide uncertainty or data-quality problems. |
| Validation/action adapter | Approved playbook and device context | Dry-run result, audit trail, controlled action | Execute raw LLM text. |

## Critical interfaces

1. **Collector to feature service:** normalized metric/event records with stable entity identifiers.
2. **Feature service to predictor:** a versioned windowed feature vector, label-free in live use.
3. **Predictor to topology correlator:** risk probabilities, feature-attribution evidence, time-horizon scores, and data quality.
4. **Correlator to RAG:** a structured incident candidate, not raw logs alone.
5. **Dashboard to action adapter:** a playbook ID and explicit operator approval, never an unconstrained natural-language instruction.
6. **Closure form to memory/training:** reviewer-verified structured labels plus a reviewed narrative.

## Source of truth

| Question | Source of truth |
|---|---|
| What is happening now? | Timestamped telemetry and application probes |
| What may happen next? | Versioned prediction model plus trend evidence |
| Where is it likely happening? | Entity IDs joined to topology and inventory |
| What has worked safely before? | Reviewed incident memory and approved runbooks |
| Can a change be made? | Human approval plus dry-run/validation result |

## MVP boundary

The MVP covers one branch-to-data-centre path and one gradual degradation scenario. It does not claim universal network support, perfect root-cause analysis, autonomous remediation, or production-grade zero-egress certification. Those are later hardening goals.

