# Packet-loss and anomaly detection design for PRAHARI

## Overview

This report explains, in simple but precise terms, what features PRAHARI needs and how each functional "block" works to detect issues like packet drops, congestion, routing instability, and how to classify them into different categories (packet loss, tunnel flap, policy drift, etc.). It is written to help you take the existing README and turn it into a concrete implementation plan.[^1][^2]

***

## What PRAHARI is trying to detect

From the README, PRAHARI’s core goal is: **predict network degradation before it becomes an incident**, and explain what is likely to fail, why, and in how much time. In SD-WAN/MPLS environments, the most common issues you care about are:[^1]

- **Packet loss / drops**: packets are transmitted but never reach the destination (link congestion, hardware faults, errors).[^2][^3]
- **High latency**: packets arrive, but too slowly for the application’s SLA (especially for voice/video).[^2]
- **Jitter**: latency varies a lot between consecutive packets, breaking real-time flows.[^4][^2]
- **Tunnel health degradation**: SD-WAN overlay tunnels start flapping, show loss/jitter spikes, or IPSec rekey anomalies.[^1][^2]
- **Routing instability**: frequent BGP/OSPF adjacency changes, route flaps, or asymmetric paths.[^2][^1]
- **Policy / configuration drift**: current config deviates from an approved baseline and causes unintended behavior.[^1]

Each incident PRAHARI detects is ultimately a combination of these low-level symptoms plus topology/context.

***

## High-level block diagram and data flow

Based on the README and SD-WAN monitoring literature, PRAHARI can be thought of as five main blocks:[^2][^1]

1. **Telemetry Ingestion Block** – Collect raw counters, logs, flows, tunnel stats, configs.
2. **Feature Extraction & Time-series Store Block** – Normalize, aggregate, and label metrics.
3. **Predictive Analytics & Anomaly Detection Block** – Forecast and detect abnormal patterns.
4. **Classification & Incident Typing Block** – Map anomalies to concrete incident categories.
5. **Copilot, RAG, and Incident Memory Block** – Explain, recommend actions, and reuse past incidents.

The NOC dashboard sits on top of blocks 3–5 to present predictions, evidence, and recommended playbooks.[^1]

***

## Block 1: Telemetry ingestion (how PRAHARI “sees” the network)

### Required inputs

The README already lists the telemetry sources; SD-WAN monitoring research confirms these as standard.[^5][^2][^1]

- **SNMP/gNMI** (polling or streaming)
  - Interface counters: input/output bytes, packets, error counters, drop counters.
  - Device status: CPU, memory, interface up/down, queue depth if exposed.
- **Syslog**
  - QoS warnings, queue drops, interface errors, routing adjacency changes.
- **Routing telemetry** (BGP/OSPF)
  - Adjacency up/down events, route advertisements/withdrawals, convergence times.[^2][^1]
- **Flow telemetry** (NetFlow/IPFIX/sFlow)
  - Per-flow packet/byte counts, application IDs, path usage, traffic directionality.[^5][^1]
- **Tunnel telemetry** (SD-WAN / IPSec)
  - Per-tunnel loss ratio, latency, jitter, rekey failures, tunnel up/down events.[^1][^2]
- **Configuration/inventory data**
  - Router, link, and tunnel topology, QoS policies, routing intents, site/asset mapping.[^1]

### What this block must do

- **Normalize formats**: convert SNMP, syslog, routing messages, and flow records into a common internal schema with consistent fields (timestamp, device, interface/tunnel, metric name, value, labels).[^5][^1]
- **Time-sync and deduplicate**: ensure all telemetry is time-aligned to a common clock and remove duplicate or corrupt records.[^5][^1]
- **Persist to time-series store**: write metrics to Prometheus/TimescaleDB (for numeric counters) and logs to Elasticsearch (for textual events), as suggested by the README.[^1]
- **Attach topology context**: when ingesting a metric, attach metadata like site, link role (CE/PE), overlay/underlay ID, VPN, application class.[^1]

This block is primarily **data engineering**: serializers, parsers, ingestion pipelines, and a schema that keeps everything aligned.

***

## Block 2: Feature extraction and labelling

Raw counters alone are not enough; PRAHARI needs features that describe network health.[^2][^1]

### Key features for packet drops and degradation

For each interface/tunnel/time window, you compute:

- **Utilization**: bytes per second / configured bandwidth.
- **Packet loss ratio**: (dropped packets) / (total transmitted packets) as derived from SNMP drop counters and tunnel telemetry.[^3][^2]
- **Latency statistics**: mean, median, max over the window.[^2]
- **Jitter statistics**: variance and peak jitter values over the window; SD-WAN systems track both average and peak jitter.[^4][^2]
- **Error rate**: CRC errors, input errors, output errors per second.
- **Queue occupancy / tail drops**: if queue depth telemetry is available, track queue utilization and drop events.[^3][^2]
- **Flow-level features**: number of active flows, per-flow packet loss, latency deviation for critical applications (VoIP, video, transactional apps).[^6][^5]

### Event and topology features

- **Routing events features**:
  - Frequency of adjacency up/down, route changes per unit time.
  - Convergence duration for routing changes.[^2][^1]
- **Tunnel events features**:
  - Tunnel flap count, rekey failures, backup-path activations.[^2][^1]
- **Configuration drift features**:
  - Boolean/score that indicates deviation from baseline config for a device, interface, QoS policy, or route.[^1]

### Labelling for training and evaluation

Because PRAHARI uses a simulated multi-site network with fault injection, every injected condition must be **labelled**:[^6][^1]

- Congestion scenario: label the time window, affected interfaces/tunnels, and the type (hub-spoke congestion, branch link congestion).
- Packet loss scenario: label whether loss is due to congestion, errors, or misconfiguration.
- Routing flap scenario: label flapping routes, affected prefixes and paths.
- Tunnel degradation scenario: label underlay issue vs overlay configuration issue.
- Policy drift scenario: label what changed vs baseline.

These labels become the ground truth used to train and evaluate your models (forecasting, anomaly detection, classifiers).

***

## Block 3: Predictive analytics and anomaly detection

This block is where PRAHARI moves from “monitoring” to **predicting** incidents before SLA impact.[^7][^1]

### Sub-blocks and required models

1. **Time-series forecasting models**
   - Inputs: per-interface/tunnel metrics (utilization, loss, latency, jitter, errors, queue depth) over past time windows.[^2][^1]
   - Models: LSTM, GRU, or Prophet-style forecasting models for each metric, as suggested in the README.[^1]
   - Output: forecasted values for the next *T* minutes (e.g., 5, 10, 30 minutes).
   - Purpose: detect if utilization or loss is **likely** to cross a critical threshold soon, not just if it already has.[^1]

2. **Topology-aware anomaly detection**
   - Inputs: graph representation of network topology and time-series features per node/edge (device, link, tunnel).[^1]
   - Models: graph-based anomaly detection (e.g., GNNs or heuristic graph algorithms) that look for correlated degradation; routing instability; path asymmetry.[^8][^9][^1]
   - Output: anomaly scores per device/link/tunnel, highlighting regions of the graph that show abnormal patterns.

3. **Tunnel health scoring**
   - Inputs: tunnel-level loss progression, jitter trends, latency shifts, rekey anomalies, tunnel flaps.[^2][^1]
   - Logic: combine these metrics into a scalar “health score” (e.g., 0–100) with thresholds for “healthy”, “warning”, “critical”. SD-WAN platforms typically track tunnel health with latency, jitter, and packet loss as primary metrics.[^2]

4. **Entropy-based precursors**
   - Inputs: routing events, syslog messages, and other discrete events over time.[^1]
   - Logic: compute Shannon entropy over event types or routing paths; sudden increases in entropy indicate unexpected disorder in routing or event patterns.

5. **Causal analysis (why-map)**
   - Inputs: a window of telemetry data around detected anomalies.[^1]
   - Models: VAR-LiNGAM or similar causal inference methods to distinguish likely cause variables (e.g., link congestion) from merely correlated symptoms (e.g., high CPU, increased RTT).[^9][^1]

6. **Calibrated ensemble**
   - Inputs: outputs from forecasting, anomaly scores, tunnel health scores, entropy analysis, and causal maps.[^1]
   - Logic: ensemble model that produces:
     - Probability of impending incident.
     - Severity (impact level on services or users).
     - Confidence (model uncertainty / calibration).
     - Estimated **time-to-impact** (lead time before serious degradation).
   - Evaluation metrics: precision, recall, false positive rate, and average lead time for detections.[^6][^1]

This block converts raw metrics into **risk predictions** with associated probabilities and timelines.

***

## Block 4: Classification and incident typing

Once anomalies and risks are identified, PRAHARI must map them into **human-understandable categories** like “packet loss due to congestion” vs “route flap cascade”.[^2][^1]

### Core incident categories

Based on the README and SD-WAN literature, incident classes include:[^10][^2][^1]

- **Hub-spoke congestion**
  - Signals: rising utilization and queue delay on hub interfaces, jitter and packet loss in traffic from multiple branches.[^2][^1]
  - Classification rule: if multiple spoke links show elevated loss/jitter and a shared hub link is near saturation, classify as hub-spoke congestion.

- **Branch link congestion**
  - Signals: high utilization and loss on a specific branch underlay; flow telemetry shows many app sessions through that link.[^10][^6]

- **Packet loss event**
  - Signals: significant increase in packet loss ratio or drop counters over baseline, with or without high utilization.[^3][^2]
  - Subtypes:
    - **Congestion-driven loss**: loss correlates strongly with high utilization / queue occupancy.[^3]
    - **Error-driven loss**: loss correlates with high CRC or physical errors, but not high utilization.[^3]
    - **Policy-driven loss**: loss occurs after a policy change or QoS misconfiguration.

- **Routing flap cascade**
  - Signals: repeated BGP/OSPF adjacency changes, frequent route withdrawals/advertisements, increased path changes for flows.[^2][^1]

- **Tunnel degradation / flap**
  - Signals: intermittent packet loss, latency spikes, jitter trends, rekey anomalies on overlay tunnels; BFD/THPP probe failures.[^2][^1]

- **Policy drift**
  - Signals: configuration diffs against baseline that correlate with new anomalies (e.g., modified QoS class, changed route maps).[^1]

- **Repeated known incident**
  - Signals: current telemetry window closely matches a previously stored incident pattern (≥85% recall in the incident memory).[^1]

### How classification works technically

For each potential incident window:

1. **Gather feature vector**: combine metrics and events for the affected scope (devices, links, tunnels, apps).[^1]
2. **Apply rule-based checks**: simple heuristics like “if utilization > X and loss > Y on a hub interface affecting multiple branches, tag as hub-spoke congestion”.[^10][^1]
3. **Apply ML classifiers**: train supervised models (e.g., gradient-boosted trees or small neural networks) on labelled scenarios to distinguish categories (congestion vs routing vs tunnel vs policy).[^8][^6]
4. **Cross-check with causal map**: ensure the primary label matches causal indicators (e.g., congestion is truly causing loss, not just correlated).
5. **Resolve conflicts**: if multiple categories apply, choose the one with highest severity/impact or present a multi-label incident with ranked causes.

Output: a structured incident object with fields like `type`, `root_cause`, `affected_scope`, and `time_to_impact`.

***

## Block 5: Copilot, RAG, and incident memory

This block translates the raw classification and prediction into **explanations and recommended actions**.[^1]

### Offline RAG copilot

- **Knowledge base contents**:
  - Topology maps, configuration baselines, runbooks, asset inventory, IP-to-location mappings, resolved incidents.[^1]
- **Retrieval**:
  - Build local embeddings (e.g., BGE) for runbooks and incident records.[^1]
  - Query the vector store with the current incident’s structured fields and telemetry summary to retrieve relevant documents and prior incidents.[^1]
- **LLM response schema**:
  - Predicted issue (e.g., “Hub-spoke congestion on MPLS core links”).
  - Crash probability and severity.
  - Time-to-impact.
  - Confidence.
  - Root-cause hypothesis.
  - Affected scope.
  - Recommended action (validated playbook steps only).
  - Evidence (citations back to telemetry, topology, and runbooks).[^1]

### Validated incident memory

- **Storage**: after an incident is resolved, store:
  - Telemetry window.
  - Root cause and category.
  - Affected scope.
  - Approved remediation steps.
  - Outcome (success/failure, residual risk).
  - References to configs and runbooks used.[^1]

- **Reuse**:
  - When a new incident arises, match its feature vector against stored incidents.
  - If similarity ≥ 0.85 recall and safety checks pass, return that previous diagnosis and playbook immediately (fast path).[^1]
  - Otherwise, route through full predictive + RAG workflow.

This block ensures the system gets **smarter over time** and preserves operator-trusted playbooks.

***

## Separating categories: mapping symptoms to incident types

To "separate in different category" (packet drop vs tunnel issue vs routing vs policy), PRAHARI needs clear **decision logic** that combines metrics and context.[^2][^1]

### Example: distinguishing packet loss categories

- If packet loss ratio increases **with** high utilization and queue depth:
  - Category: **Congestion-induced packet loss**.
- If packet loss ratio increases but utilization is moderate and errors (CRC, physical) spike:
  - Category: **Physical/error-induced packet loss**.
- If packet loss begins immediately after a config change (QoS policy, ACL, route-map) and occurs mostly for specific apps/flows:
  - Category: **Policy-induced packet loss**.

In each case, the incident object still has common fields (loss ratio, affected flows) but a different root cause and recommended remediation.

### Example: differentiating tunnel vs underlay problems

- If overlay tunnel shows high loss/jitter **and** underlay link counters show congestion and drops:
  - Classify as **underlay congestion affecting overlay**.
- If overlay tunnel shows loss/jitter but underlay counters are normal, and rekey anomalies appear:
  - Classify as **overlay/tunnel configuration or crypto issue**.

This separation is crucial, because remediation differs (change path/QoS vs fix IPSec/tunnel settings).[^2]

### Example: routing instability vs congestion

- Routing instability:
  - Frequent OSPF/BGP adjacency changes, route flaps, path switches.[^2][^1]
  - Latency/jitter may vary as paths change, but link utilization might stay moderate.
- Congestion:
  - Stable routing, but high utilization, queue depth, and loss on specific links.[^6][^3]

Combining event telemetry and link metrics lets PRAHARI decide whether to recommend stabilizing routing or redistributing traffic/QoS.

***

## Minimum viable feature set for a working prototype

Given hackathon constraints, you do not need to implement all ideas at once. A practical MVP could focus on:[^10][^6][^1]

1. **Telemetry ingestion**
   - SNMP interface counters (bytes, packets, drops, errors).
   - Simple tunnel metrics (loss, latency, jitter) from SD-WAN endpoints.
   - Syslog events for interface up/down and routing changes.

2. **Basic feature computation**
   - Utilization, packet loss ratio, latency and jitter statistics per interface/tunnel.
   - Simple rolling averages and deviations (moving mean/variance).

3. **Simple forecasting / detection**
   - Use Prophet or basic ARIMA/LSTM models to forecast utilization and loss.
   - Flag potential “congestion soon” when forecasted utilization exceeds X% and projected loss ratio exceeds Y%.

4. **Rule-based classification**
   - Implement heuristic rules for a few scenarios:
     - Hub-spoke congestion.
     - Branch link congestion.
     - Tunnel degradation.
     - Routing flap cascade.

5. **Copilot UI and explanations**
   - Build a small React dashboard that:
     - Shows topology.
     - Displays predicted incidents as cards.
     - Presents evidence (graphs of utilization/loss/jitter over time).
     - Suggests simple playbooks (e.g., “shift traffic to backup link”, “reduce non-critical QoS class”).

Later iterations can add graph-based anomaly detection, causal analysis, entropy precursors, and a full RAG knowledge base.[^8][^1]

***

## Implementation checklist (step-by-step)

To make it concrete, here is a stepwise plan aligned with the README and literature:[^6][^5][^1]

1. **Build or reuse a Containerlab SD-WAN/MPLS lab**
   - Include at least: hub, a few branches, MPLS core, SD-WAN tunnels.
   - Configure basic QoS and routing.

2. **Wire up telemetry collection**
   - Use Telegraf/Vector to collect SNMP, syslog, NetFlow/IPFIX, and tunnel stats.
   - Stream metrics into Prometheus/TimescaleDB; logs into Elasticsearch.

3. **Define schemas and feature extraction jobs**
   - Decide on standard fields: `timestamp`, `device_id`, `interface_id/tunnel_id`, `metric_name`, `value`, `labels`.
   - Implement periodic jobs (Python/FastAPI workers) to compute per-window features and store them.

4. **Implement simple forecasting/anomaly detection**
   - Train Prophet/LSTM models on utilization and loss per interface/tunnel.
   - Implement basic anomaly rules (e.g., z-score, threshold + trend).

5. **Implement incident classification logic**
   - Encode rules for hub-spoke congestion, tunnel degradation, routing flap cascade, etc.
   - Later, add ML-based classification using labelled scenarios.

6. **Build the dashboard and copilot interface**
   - React front-end for topology graph, incident cards, lead-time countdowns, and evidence graphs.
   - FastAPI backend endpoint that returns structured incident objects and explanations.

7. **Add RAG and incident memory when time allows**
   - Create a small vector store with runbooks and lab incidents.
   - Run a local LLM (Llama/Qwen/Phi) via Ollama or `llama.cpp`.
   - Implement schema-constrained responses.

8. **Validate scenarios**
   - Use `tc netem`, `iperf3`, TRex to generate labelled conditions.
   - Measure detection accuracy, false positives, and average lead time.

This roadmap turns the abstract README into concrete coding tasks you can distribute across a team.[^1]

---

## References

1. The original external upload link was removed because it contained temporary credentials. This historical note is retained for background only; use the repository [README](README.md) and [documentation index](docs/INDEX.md) for current material.

## Air-Gapped Predictive NOC Copilot for Secure SD-WAN/MPLS Operations

**Tagline:** Pred...

2. [Analysis of SD-WAN Architectures and Techniques for ... - PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC12567460/) - by J Dudczyk · 2025 · Cited by 9 — For QoS, it highlights jitter management, packet loss mitigation,...

3. [Microsoft Word - e2emon2005-postech-paper-final.doc](https://www.cs.purdue.edu/nsl/e2emon2005-postech-paper-final.pdf)

4. [Jitter Trends | SD-WAN Orchestrator Docs - HPE Aruba Networking](https://arubanetworking.hpe.com/techdocs/sdwan/docs/orch/monitoring/tunnel-health/jitter-trends/) - These pages provide technical guidance about specific areas of functionality in the Aruba EdgeConnec...

5. [Network Monitoring: SNMP, NetFlow, sFlow, gNMI ... - SiamLancard](https://www.siamlancard.com/network-monitoring-snmp-netflow-sflow-gnmi-streaming-telemetry-observability-stack-guide/) - Network Monitoring: SNMP, NetFlow, sFlow, gNMI, Streaming Telemetry และ Observability Stack Network ...

6. [Adaptive WAN Link Anomaly Detection Using Lightweight Packet ...](https://ijsrmt.com/index.php/ijsrmt/article/view/1348?articlesBySimilarityPage=5) - Maintaining stable Wide Area Network (WAN) connectivity between branch offices and centralized headq...

7. [How Cisco SD-WAN Analytics & Insights Powers Faster ...](https://www.ciscolive.com/c/dam/r/ciscolive/global-event/docs/2024/pdf/BRKENT-2469.pdf) - Collect network telemetry via SD-WAN Analytics 2. Data Analysis Predictive modeling to forecast issu...

8. [Machine Learning-Based Network Anomaly Detection](https://oa.upm.es/88684/1/10316838.pdf)

9. [Network anomaly detection and performance evaluation of ...](https://www.sciencedirect.com/science/article/pii/S1877050924008871)

10. [SD-WAN Monitoring — Analytics, Dashboards & ...](https://www.networkershome.com/fundamentals/sd-wan/sd-wan-monitoring-analytics-troubleshooting/) - Common issues include tunnel flaps, packet loss, or high latency. To diagnose, use commands like sho...
