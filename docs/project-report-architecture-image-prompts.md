# PRAHARI project-report architecture image prompts

These prompts are written for diagram/image-generation models. Generate one image at a time. Do not paste every prompt into one request.

## Architecture explanation in simple language

PRAHARI is a self-hosted predictive network monitoring and operator-assistance platform. It keeps four concerns separate:

1. **Data plane:** real application traffic such as chat and verified file transfer travels between a branch user and a data-centre application.
2. **Management plane:** collectors, the API, database, feature computation, prediction logic, local knowledge retrieval, and dashboard observe the network over a separate management path.
3. **Control and safety plane:** PRAHARI can recommend a reviewed playbook, but a human must review current evidence, run a dry-run, approve a fixed action, and validate the result. Natural-language model output never becomes a router command.
4. **Knowledge plane:** only checksum-approved runbooks and independently reviewed closure cases may support an operator brief.

The current demonstration has two evidence sources that must never be visually mixed:

- a real two-laptop application test that measures browser-to-service RTT, upload throughput, received byte count, and SHA-256 integrity without retaining file or chat content; and
- replay, safe synthetic telemetry, or isolated Containerlab telemetry used by the current heuristic prediction baseline.

The current predictor computes windowed statistics such as slope, EWMA, robust z-score, baseline deviation, and data-quality checks. It reports incident type, confidence, likely location, evidence, and impact risk within 30, 60, and 120 seconds. An approved LightGBM bundle is supported, but the normal demonstration must identify its current heuristic/replay status honestly.

## Shared visual instructions

Add this block to every prompt if the image model accepts long prompts:

```text
Create a publication-quality technical architecture diagram for a final-year engineering project report. Use a clean white background, flat vector shapes, thin consistent connector arrows, no 3D objects, no photorealistic computers, no decorative cyberpunk effects, and no meaningless charts. Use PRAHARI colours: deep navy #0F2F61, primary blue #0B70F5, light blue #EAF3FF, healthy green #12A65A, warning amber #F3A20C, critical red #EF233C, grey borders #D8E4F2, and dark text #0B1F44. Use Inter or a similar clean sans-serif font. Use short readable labels, consistent spacing, and a small legend. Use solid arrows for primary data flow, dotted arrows for telemetry/observation, green arrows for validated success, amber arrows for human review, and red only for faults or blocked actions. Output a straight front-facing diagram, 16:9 landscape, 4K resolution, with generous margins suitable for an A4 report. The title must be readable. Do not add technologies, cloud services, vendor products, or claims that are not explicitly listed. Do not claim autonomous remediation, guaranteed prediction, production accreditation, defence accreditation, MPLS validation, or universal network support.
```

---

## Figure 1 — Complete logical architecture

**Report section:** System Architecture  
**Recommended caption:** *Logical architecture of PRAHARI, separating the application data plane, out-of-band management plane, knowledge plane, and human-controlled response plane.*

```text
Create a complete logical architecture diagram titled “PRAHARI — Self-Hosted Predictive Network Assurance Architecture”.

Divide the diagram into four clearly labelled horizontal zones.

Zone 1, Data Plane:
- Branch User / Application Client
- Branch Edge Router
- Primary Routed Path
- Backup Routed Path
- Data-Centre Edge Router
- Chat and File Application Server
- Show chat messages and file bytes travelling from Branch User through the primary path to the application server.
- Show the backup path as available but inactive.

Zone 2, Out-of-Band Management and Analytics Plane:
- Telemetry Collector
- Validation and Normalisation
- Durable SQL Evidence Store
- 60-Second Feature Window and Data-Quality Checks
- Predictor: Heuristic Trend Baseline or Approved LightGBM Bundle
- Risk at 30 / 60 / 120 Seconds
- Topology Correlator
- Incident Service
- FastAPI Backend
- React Observability Dashboard

Draw dotted telemetry arrows from branch edge, data-centre edge, and application probes to the collector. Draw the processing sequence from collector to validation, SQL store, feature window, predictor, topology correlator, incident service, FastAPI, and dashboard.

Zone 3, Local Knowledge Plane:
- Checksum-Approved Runbooks
- Reviewed Closure Cases / Incident Memory
- Local Retrieval
- Optional Offline Local LLM
- Cited Operator Brief

Connect approved runbooks and reviewed cases to local retrieval. Connect retrieval to the optional local LLM and then to a cited operator brief in FastAPI. Add a boundary note: “The language model explains selected verified evidence; it is not the prediction engine and cannot create router commands.”

Zone 4, Human-Controlled Safety Plane:
- Operator Reviews Current Prediction ID
- Dry-Run Safety Check
- Explicit Human Approval
- Fixed Lab Action Adapter
- Post-Change Validation
- Rollback if Validation Fails
- Append-Only Audit Record
- Independent Closure Review

Connect the dashboard to the operator review. Show that the action path is blocked until review, dry-run, and approval succeed. Connect the fixed action adapter only to the isolated lab routers. Add a red blocked arrow from “Raw LLM Text” to “Network Device” labelled “Never permitted”.

Add a legend:
- solid blue: application or API data
- dotted blue: telemetry
- amber: human decision
- green: validated action or recovery
- red: fault or blocked unsafe path

Add a footer note: “Current pilot: real application measurements plus replay, safe simulation, or isolated lab telemetry. Production claims require target-specific evaluation and approval.”
```

## Figure 2 — Two-laptop live demonstration architecture

**Report section:** Demonstration Setup  
**Recommended caption:** *Two-laptop application-measurement demonstration using a private hotspot without changing the college or physical network.*

```text
Create a clear deployment diagram titled “PRAHARI Two-Laptop Live Application Demo”.

Show a private phone hotspot or isolated private LAN in the centre. Do not show the public internet as required.

On the left, show Laptop B labelled “User Laptop — Browser Only”. Inside it show:
- User Link Web Page
- Access Code Kept in Browser Memory
- Send Test Message
- Select Small Test File

On the right, show Laptop A labelled “PRAHARI Demonstration Host”. Inside it show four local services:
1. User Demo Service, private LAN address, TCP port 8780
2. PRAHARI FastAPI Collector and Analytics API, loopback 127.0.0.1:8000
3. React / Vite Operator Dashboard, loopback 127.0.0.1:5173
4. Local SQL Database

Draw browser traffic from Laptop B to the User Demo Service on Laptop A. Draw an internal loopback arrow from the User Demo Service to the collector API. Draw API arrows from the dashboard to FastAPI and from FastAPI to the SQL database.

Show the measured evidence beside the transfer arrow:
- browser round-trip time
- application jitter
- upload throughput
- attempted and received message count
- file byte count
- SHA-256 integrity receipt

Show a privacy shield beside Laptop A with these exact points:
- Chat text is not logged
- File content is hashed in memory
- Content is discarded after verification
- Only measurements and the integrity receipt are stored

Show an access-control shield:
- Fresh random access code
- Private IP binding only
- No router port forwarding
- Collector and dashboard stay on loopback

Add a clearly separated note box titled “What this proves”: “Two real laptops exchange application data and PRAHARI records service measurements.”

Add another note box titled “What this does not prove”: “This alone does not prove future outage prediction; prediction requires time-series network telemetry and held-out evaluation.”

Add a safety footer: “No packet-loss injection, route change, network scan, denial of service, or college-network modification.”
```

## Figure 3 — Isolated network-lab topology

**Report section:** Experimental Network Environment  
**Recommended caption:** *Validated Containerlab/FRRouting OSPF testbed with separate primary, backup, and management paths.*

```text
Create a network topology diagram titled “PRAHARI Isolated Containerlab / FRRouting Testbed”.

Draw the data path left to right:
Branch Client — Branch Edge — PE Primary — Core — PE Secondary — DC Edge — Application Server.

Label this solid upper route “Primary path — lower OSPF cost”.

Draw a lower backup route:
Branch Edge — PE Backup — DC Edge.

Label it “Backup path — higher OSPF cost”.

Mark Branch Edge eth1 with a controlled red fault symbol and label it “Allow-listed netem fault point: gradual loss, delay, or jitter”. Mark Branch Edge eth2 as “Backup egress”.

Use these network labels where space permits:
- Branch LAN 172.16.10.0/24
- Primary links 10.10.0.0/30 through 10.10.3.0/30
- Backup links 10.20.0.0/30 and 10.20.1.0/30
- Application LAN 172.16.20.0/24

Below the data path, draw a separate dotted management network labelled “Out-of-band Containerlab management network 172.31.255.0/24, no external access”. Connect every node’s management interface to this network. Add a NOC API node on the management network.

Show the NOC API receiving application probes, path telemetry, and experiment labels without using the degraded data path for control.

Add a small controlled-experiment sequence:
1. Healthy baseline
2. Bounded gradual impairment
3. Detection and prediction
4. Explicit fault cleanup
5. Recovery measurement

Add a warning banner: “Validated as an isolated routed FRR/OSPF lab. It is not yet an MPLS data plane, vendor SD-WAN overlay, or production network.”
```

## Figure 4 — Telemetry-to-prediction data pipeline

**Report section:** Data Processing Pipeline  
**Recommended caption:** *From validated network and application observations to an explainable incident prediction.*

```text
Create a left-to-right data pipeline diagram titled “PRAHARI Telemetry and Prediction Pipeline”.

Use seven numbered stages:

1. Sources
- Router and interface observations
- Tunnel or path metrics
- Routing and lifecycle events
- Chat application probes
- File-upload probes
- Versioned topology and inventory

2. Admission and Validation
- Pydantic schema validation
- Stable entity IDs
- UTC timestamp validation
- Duplicate handling
- Allowed source identity
- Payload and request-size limits
- Data-quality flags

3. Durable Evidence Store
- telemetry_records
- app_probe_records
- event_records
- topology and configuration version references

4. Windowed Feature Computation
- 60-second window
- sample count
- latest and mean
- p95 and standard deviation
- slope per second
- EWMA
- baseline median
- robust z-score
- freshness and data-quality status

5. Prediction
- current heuristic trend baseline OR independently approved LightGBM bundle
- incident-type probabilities
- risk within 30 seconds
- risk within 60 seconds
- risk within 120 seconds
- confidence and model version
- estimated time-to-impact range

6. Topology Correlation
- monitored entity to interface
- interface to path
- path to sites and applications
- ranked likely root-location candidates
- primary and backup relationship

7. Incident Output
- incident ID and prediction ID
- likely incident type
- severity
- affected scope
- evidence statements
- data-quality limitations
- operator dashboard and audit trail

Use blue arrows between stages. Show invalid data leaving Stage 2 through a red rejected-record path labelled “Rejected explicitly; never silently accepted”. Show low-quality data producing “Detection unavailable or uncertainty shown”, not a green healthy result.

Add a bottom rule: “Live feature computation is label-free. Training labels and fault-phase names are never passed into live prediction.”
```

## Figure 5 — Prediction-engine internals

**Report section:** Predictive Model Design  
**Recommended caption:** *Explainable multi-horizon prediction contract and approval boundary.*

```text
Create an explainable machine-learning architecture diagram titled “PRAHARI Prediction Engine”.

On the left show a feature vector grouped into:
- loss and jitter features
- latency and throughput application features
- slopes and EWMA trends
- baseline deviations and robust z-scores
- sample count, freshness, and quality
- topology/profile version

In the centre show two selectable predictor implementations behind one versioned interface:
A. “Current explainable heuristic baseline” using trend scores and provisional threshold-crossing estimation.
B. “Approved predictive-v2 LightGBM bundle” using a signed model artifact and manifest only after held-out evaluation and independent approval.

Do not portray both as active simultaneously. Show a model-selection gate labelled “Environment + model approval + exact network-profile SHA-256”.

On the right show the prediction output contract:
- normal versus tunnel-degradation probability
- impact risk at 30, 60, and 120 seconds
- confidence
- time-to-impact range and basis
- model version and feature version
- network-profile SHA-256
- evidence / feature attribution
- data-quality status

Below, show evaluation gates:
- train/validation/test runs separated by run ID
- no leakage between related runs
- false-positive rate
- precision and recall
- Brier score / calibration
- lead-time distribution
- unseen gradual-ramp fault runs
- approval record and change ticket

Draw a red blocked path labelled “No approval or profile mismatch → monitoring only / fail closed”.

Add a note: “The local language model is not part of this prediction calculation.”
```

## Figure 6 — Incident lifecycle and safe response workflow

**Report section:** Operational Workflow  
**Recommended caption:** *Prediction-bound, human-controlled incident review and action lifecycle.*

```text
Create a state-machine and workflow diagram titled “PRAHARI Incident Lifecycle and Human Safety Gate”.

Show these states in order:
WATCH → PREDICTED → REVIEWED → MITIGATING → VALIDATING → RESOLVED.

Also show side exits:
- BLOCKED
- ESCALATED
- FALSE POSITIVE or UNCERTAIN as reviewed closure outcomes

Add required transitions:
1. Current telemetry produces a prediction ID.
2. Operator opens evidence and data-quality details.
3. Operator records a review note bound to the exact prediction ID.
4. System retrieves a fixed approved playbook ID.
5. Dry-run validates permissions, evidence freshness, topology, adapter status, and rollback readiness.
6. Explicit human approval is accepted only for the same actor and same current prediction evidence.
7. Fixed action adapter applies only an allow-listed lab action.
8. Post-change application and network validation runs.
9. If validation fails, rollback or escalation occurs.
10. Operator submits a structured closure outcome.
11. A different reviewer independently approves closure before the case becomes reviewed incident memory.

Draw a large shield around steps 3 through 9 labelled “Human-controlled safety boundary”.

Show these prohibited paths with red stop symbols:
- LLM text directly to router
- stale prediction approval
- approval without dry-run
- production use of the controlled-lab adapter
- unreviewed closure automatically entering incident memory

Show every state transition writing to the append-only audit trail.
```

## Figure 7 — Local RAG and incident-memory architecture

**Report section:** Explainable NOC Copilot  
**Recommended caption:** *Bounded local retrieval that produces cited explanations without controlling the network.*

```text
Create a diagram titled “PRAHARI Local Knowledge Retrieval and Cited Copilot”.

On the left show approved knowledge inputs:
- checksum-approved runbooks
- versioned topology and inventory
- configuration baselines
- independently reviewed closure cases

Before retrieval, show an approval catalogue gate with:
- relative safe path
- SHA-256 checksum
- reviewer ID
- approval reference
- reject traversal, symlink, missing file, or checksum mismatch

In the centre show:
- local document chunking
- local embeddings / bounded retrieval index
- structured current incident candidate
- evidence selection
- optional offline Ollama or llama.cpp model

On the right show a cited operator brief containing:
- situation
- root-cause hypothesis
- impact
- recommended playbook ID
- citations to exact source IDs
- confidence
- limitations

Show the API verifying that generated statements select only supported statement IDs. Draw a red rejected path for uncited or unsupported claims.

At the bottom add three strong rules:
1. “The copilot explains; the predictor predicts.”
2. “Similarity to an old case is not proof that an action is safe.”
3. “Copilot output cannot execute a command.”
```

## Figure 8 — Data-storage architecture

**Report section:** Database Design  
**Recommended caption:** *Durable evidence, workflow, authentication, and audit records in the PRAHARI SQL store.*

```text
Create a conceptual data-storage diagram titled “PRAHARI Durable SQL Evidence Model”.

Show a central SQL database. Label development as SQLite and production-shaped deployment as PostgreSQL with Alembic migrations.

Draw these tables as clean entity boxes:
- telemetry_records
- app_probe_records
- event_records
- incident_states
- action_operations
- audit_events
- closure_cases
- browser_sessions
- login_windows

Show conceptual relationships:
- telemetry_records + app_probe_records + event_records feed an incident prediction
- incident_states references the current prediction snapshot
- action_operations references incident ID, prediction ID, playbook ID, actor, status, and reconciliation evidence
- audit_events records identity, event type, detail, and timestamp and is append-only for the runtime role
- closure_cases stores prediction snapshot, evidence snapshot, outcome, reviewer decision, and network-profile metadata
- browser_sessions and login_windows support authenticated local browser access and rate limiting

Add database-role boundaries:
- migrator role owns schema changes
- runtime API role has only required SELECT / INSERT / UPDATE permissions
- audit_events allows SELECT and INSERT only, no runtime UPDATE or DELETE
- browser session expiry permits controlled DELETE only for session records

Add a note: “Uploaded file bodies and chat text are not stored in these tables.”
```

## Figure 9 — Backend software architecture

**Report section:** Software Component Design  
**Recommended caption:** *Modular FastAPI backend with strict contracts, persistence, analytics, copilot, authentication, and guarded actions.*

```text
Create a layered software-component diagram titled “PRAHARI FastAPI Backend Architecture”.

Layer 1, Interfaces:
- REST API
- Browser session endpoints
- Telemetry collector endpoints
- Traffic-demo summary endpoint
- Health, readiness, and Prometheus metrics endpoints

Layer 2, Cross-Cutting Admission Controls:
- request-size limits
- content-type checks
- authentication and RBAC
- CSRF / Origin validation for browser actions
- concurrency and timeout limits
- response schema validation

Layer 3, Application Services:
- Prahari Service / Incident Orchestration
- Telemetry Collector Service
- Feature and Data-Quality Service
- Prediction Service
- Topology and Network-Profile Service
- Copilot / Evidence Selection Service
- Review and Closure Service
- Action Operation and Reconciliation Service
- Retention Service

Layer 4, Domain Modules:
- typed Pydantic contracts
- trend baseline and LightGBM adapter
- topology correlation
- approved playbook policy
- fixed action adapter client
- browser identity and roles

Layer 5, Infrastructure:
- SQLAlchemy persistence repository
- SQLite for local development
- PostgreSQL for production-shaped deployment
- Alembic migrations
- local model artifacts and manifests
- approved knowledge directory
- optional local LLM endpoint

Use arrows showing interfaces call application services, services use domain modules, and repositories/adapters cross into infrastructure. Show all outbound integrations behind allow-listed private endpoints and explicit configuration. Add a note: “Fail closed on missing migrations, invalid profiles, unapproved models, stale evidence, and uncertain action results.”
```

## Figure 10 — Dashboard information architecture

**Report section:** User Interface Design  
**Recommended caption:** *PRAHARI Observability Studio presents service status, prediction evidence, topology, and human-controlled operations in one interface.*

```text
Create a clean UI information-architecture diagram titled “PRAHARI Observability Studio”.

Show a hamburger navigation menu leading to:
- Overview
- Network
- Applications
- Incidents
- Topology
- Analytics
- Reports
- Configuration

Show the Overview page divided into these sections:
1. Utility header: data-source badge, API state, last refresh, refresh control, operator identity
2. Status strip: Dashboard API, Telemetry, Application Link, Active Alerts, Automatic Refresh
3. Fault Detection: severity, incident type, 120-second risk, time to impact, likely location, data quality, confidence, source badge
4. Risk of Impact: 30 / 60 / 120-second visual trend
5. Application Test: filename, verified bytes, latency, throughput, SHA-256, sender, receiver, transport, privacy note
6. Key Network Telemetry: packet loss, jitter, application RTT, throughput, source labels and sparklines
7. Network Topology: branch, primary path, fault marker, backup path, data centre
8. Incident Investigation: risk horizons, scope, model provenance, evidence, recommendation
9. Audit Timeline
10. Detailed Operator Workflow: review, dry-run, approval, closure, action reconciliation

Show visible modes and states:
- Live application measurement
- Live telemetry
- Recorded replay
- Safe simulation
- Loading
- Detection unavailable
- API disconnected
- No measurement

Add a UI rule: “Never show ‘No fault’ when telemetry is unavailable, and never label replay or simulation as live.”
```

## Figure 11 — Production-shaped container deployment

**Report section:** Deployment Architecture  
**Recommended caption:** *Fail-closed single-host deployment scaffold with isolated networks, least-privilege identities, migrations, and file-backed secrets.*

```text
Create a container deployment diagram titled “PRAHARI Production-Shaped Local Deployment”.

Show one authorised Linux host with two Docker bridge networks:
- web_ingress: only the dashboard gateway joins; host binding defaults to 127.0.0.1; outbound masquerading disabled
- app_internal: internal network containing API and PostgreSQL

Show containers and startup order:
1. PostgreSQL becomes healthy
2. Short-lived Alembic migrate job upgrades schema
3. Short-lived grant-app-role job applies exact runtime table permissions
4. FastAPI service starts as numeric non-root UID/GID 10001, read-only root filesystem, all Linux capabilities dropped, no-new-privileges
5. Nginx/static React dashboard starts as non-root and exposes the single loopback host port

Show file-backed secrets mounted only to the service that needs each secret:
- PostgreSQL bootstrap password
- migrator database URL
- runtime application database URL
- API-key JSON or browser-account file

Show read-only external artifacts:
- approved network profile
- approved knowledge directory
- model artifact and signed manifest
- release acceptance evidence

Add labels:
- pinned and preloaded container images
- pull policy never
- health checks and resource limits
- local logs and Prometheus metrics
- backup and restore evidence
- TLS reverse proxy required for non-loopback production access

Add a red fail-closed box listing: missing secrets, pending migrations, unapproved model, profile checksum mismatch, missing release acceptance, or unsafe production lab adapter.

Add a footer: “This is a production-shaped scaffold. Target-specific operating, security, model, and acceptance evidence is still required before production or defence-sector claims.”
```

## Figure 12 — Live file-transfer sequence diagram

**Report section:** Application Measurement Sequence  
**Recommended caption:** *Sequence of a privacy-preserving two-laptop file and chat measurement.*

```text
Create a UML-style sequence diagram titled “PRAHARI Two-Laptop Application Measurement Sequence”.

Use these lifelines from left to right:
- User on Laptop B
- Browser User Link
- Laptop A User Demo Service
- PRAHARI Collector API
- SQL Evidence Store
- Operator Dashboard

Sequence:
1. User enters a fresh access code; browser keeps it only in memory.
2. Browser sends an authenticated connection check.
3. User Demo Service validates private binding, token length, token value, rate and size limits.
4. Browser sends several harmless chat probes.
5. Service returns acknowledgements; browser/service calculates real round-trip samples.
6. User selects a small file and starts transfer.
7. Service receives opaque bytes in memory, counts bytes, and calculates SHA-256.
8. Service verifies exact received size and integrity.
9. Service discards content unless an explicitly bounded temporary download is offered.
10. Service posts only normalised app-probe records and lifecycle events to the collector API over loopback.
11. API validates records and stores measurement evidence.
12. Dashboard polls the API and displays verified file size, latency, jitter, throughput, and checksum status.

Show error alternatives:
- wrong access code → 401 rejection
- file over 5 MiB → local limit rejection
- collector unavailable → transfer can verify locally, but dashboard evidence is explicitly not saved

Add a privacy note: “No chat text or uploaded file body is included in dashboard/API evidence.”
```

## Figure 13 — Fault prediction and response sequence

**Report section:** End-to-End System Operation  
**Recommended caption:** *From controlled degradation to prediction, human decision, validation, and reviewed closure.*

```text
Create a UML-style sequence diagram titled “PRAHARI End-to-End Fault Prediction and Controlled Response”.

Use these lifelines:
- Application Probe
- Branch / DC Network Path
- Telemetry Collector
- SQL Evidence Store
- Feature Service
- Predictor
- Topology Correlator
- FastAPI / Incident Service
- NOC Dashboard
- Human Operator
- Fixed Lab Action Agent
- Independent Reviewer

Sequence:
1. Healthy application traffic and network measurements establish a baseline.
2. An authorised isolated-lab script gradually increases loss, delay, or jitter at the allow-listed primary interface.
3. Collector validates and stores timestamped network and application observations.
4. Feature service creates a 60-second window and checks freshness and sample quality.
5. Predictor calculates incident probability, confidence, 30/60/120-second risk, and time-to-impact range.
6. Topology correlator ranks the likely interface and affected path/sites/applications.
7. Incident service creates or updates an incident bound to a prediction ID and evidence window.
8. Dashboard shows the fault, source mode, uncertainty, evidence, and recommendation.
9. Human operator reviews evidence and submits a note.
10. System runs a fixed playbook dry-run.
11. Operator explicitly approves only if evidence is current and dry-run passed.
12. Fixed lab action agent performs the allow-listed action; no natural-language command is accepted.
13. Application and network probes validate recovery.
14. If validation fails, block success and offer rollback or escalation.
15. Operator records closure outcome and evidence.
16. Independent reviewer approves or rejects the closure.
17. Only approved closure becomes incident memory; automatic model retraining does not occur.

Add red stop markers for stale evidence, unconfirmed action result, failed validation, and production use of a lab-only adapter. Show every major step writing an audit event.
```

## Figure 14 — Current implementation versus future validated deployment

**Report section:** Scope, Limitations, and Future Work  
**Recommended caption:** *Honest separation of implemented capability, controlled pilot capability, and future customer-specific validation.*

```text
Create a three-column maturity diagram titled “PRAHARI Capability and Validation Boundary”.

Column 1, Implemented Now:
- FastAPI typed API
- React Observability Studio
- SQLite and PostgreSQL persistence support
- Alembic migrations
- two-laptop chat/file measurement
- checksum and byte-count verification
- replay and safe telemetry simulations
- heuristic 30/60/120-second risk baseline
- topology correlation
- evidence and data-quality display
- RBAC and browser sessions
- dry-run and approval workflow
- audit, closure cases, independent closure review
- production-shaped Docker Compose scaffold

Column 2, Controlled Isolated Pilot:
- Containerlab and FRRouting routed OSPF topology
- primary and higher-cost backup path
- bounded tc netem fault injection on one allow-listed lab interface
- real labelled telemetry experiments
- local model artifact evaluation
- optional local LLM with cited statements
- fixed lab action agent with rollback and validation

Column 3, Required Before Real Production or Defence-Sector Claim:
- target router and controller integration
- customer network profile and approved telemetry mapping
- independently collected training and held-out fault runs
- calibrated and independently approved model
- TLS, managed identity, secrets, firewall, retention and backup policy
- threat model, penetration/security testing and vulnerability acceptance
- high-availability and disaster-recovery testing
- performance, load, failure and recovery evidence
- customer change-control and action-adapter approval
- external accreditation or authority-to-operate process

Use green checks for implemented items, amber laboratory symbols for controlled pilot items, and blue gate symbols for future requirements. Do not use red crosses for future work; show them as formal gates. Add a footer: “Repository evidence supports a controlled engineering prototype and production-oriented foundation, not automatic accreditation.”
```

## Final negative prompt

Append this when a generator adds imaginary technology or marketing claims:

```text
Negative prompt: no AWS, Azure, Google Cloud, Kubernetes, Kafka, Splunk, Cisco logo, proprietary vendor logo, satellite, globe, robot, humanoid AI, SOC stock photo, hacker silhouette, neon cyberpunk, 3D server rack, blockchain, autonomous remediation, guaranteed 100% accuracy, live production label, defence-certified badge, military insignia, weapon imagery, unlabelled cloud, invented database, invented microservice, or invented protocol. Do not merge live application evidence with replay or synthetic prediction evidence. Do not show the local language model as the predictor or action executor.
```

## Report-quality checklist

Before adding any generated image to the report, verify:

1. Every label is spelled correctly; image models often corrupt technical text.
2. Live, replay, safe simulation, and isolated lab evidence are visibly distinct.
3. The LLM is shown only in the knowledge/explanation path.
4. Human review, dry-run, approval, validation, rollback, and audit gates are visible.
5. The two-laptop demo does not claim it predicts an internet-scale outage.
6. The network lab says routed FRR/OSPF, not validated MPLS or commercial SD-WAN.
7. The production diagram says production-shaped scaffold, not production accredited.
8. File bodies and chat content are not shown as stored data.
9. The figure number, title, caption, and source are added in the report.
10. Export as SVG or high-resolution PNG; keep editable diagram source when possible.

