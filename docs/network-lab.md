# Network lab and real application traffic

## Demo objective

The lab must prove that PRAHARI protects traffic that users can see. The traffic path carries a local chat message, image/file transfer, and video stream or video-quality probe. The dashboard is not the application being protected; it observes the actual network path.

## Recommended MVP topology

~~~mermaid
flowchart LR
    BC[Branch client<br/>chat, upload, video probe] --> BR[Branch edge]
    BR --> PE1[Provider edge A]
    PE1 --> P[Provider core]
    P --> PE2[Provider edge B]
    PE2 --> DC[DC edge]
    DC --> AS[Application server]

    BR -. overlay tunnel, primary .-> DC
    BR -. overlay tunnel, backup .-> DC

    MGMT[Management network<br/>collector, dashboard, controller] -. read-only telemetry .-> BR
    MGMT -. read-only telemetry .-> DC
    MGMT -. read-only telemetry .-> PE1
    MGMT -. read-only telemetry .-> PE2
~~~

The primary path is the path intentionally degraded in the demo. The backup path remains healthy and is used only after an operator-approved, controlled traffic shift or failover.

## Node roles

| Node | Role | Minimum responsibilities |
|---|---|---|
| branch-client | Local user endpoint | Sends chat, upload, and video/probe traffic. |
| branch-edge | Branch SD-WAN edge | Chooses primary/backup path, exposes tunnel/link telemetry. |
| pe-a, core, pe-b | MPLS underlay nodes | Carry routed/MPLS traffic and expose routing/interface health. |
| dc-edge | Hub/data-centre edge | Terminates the remote side of the paths. |
| app-server | Local service endpoint | Hosts the demo application and application-health probe. |
| collector | Management-plane service | Polls/subscribes to telemetry and receives syslog. |
| prahari services | Management-plane services | Store, predict, retrieve, dashboard, and audit. |

## Implementation options

Use a real Linux host or Linux virtual machine for the network lab. Containerlab and FRRouting are suitable for the underlay. MPLS needs Linux kernel support, so do not rely on a restricted cloud devcontainer.

For the SD-WAN-like overlay, select one option and document it in the eventual topology:

| Option | Appropriate use |
|---|---|
| FRRouting MPLS underlay plus Linux/VyOS tunnel endpoints | Open, reproducible MVP where tunnel state and path changes are controllable. |
| Vendor virtual SD-WAN edges/controller | Stronger vendor realism when images and licences are available. |
| BGP/OSPF-only lab first | Valid early telemetry/ML milestone, but do not call it a full SD-WAN/MPLS demo. |

The demo should use the first option unless the team already has lawful access to vendor images.

## Data path versus management path

Do **not** put PRAHARI monitoring traffic in the same path being deliberately broken.

| Network | Carries | Why |
|---|---|---|
| Primary data path | User chat, image/file transfer, video, normal application probes | This is the path whose health PRAHARI predicts. |
| Backup data path | Same application traffic after controlled failover | Demonstrates a safe mitigation. |
| Out-of-band management path | SNMP/gNMI, syslog, collector, dashboard, fault controls | Keeps the demo observable during a primary-path incident. |

## Application traffic for the demo

Use one small local web application with all three capabilities, or separate local tools if that is faster:

| Visible activity | Measurement | Why it matters |
|---|---|---|
| Chat message | Round-trip response time and delivery success | Easy for an audience to understand. |
| Image/file upload | Transfer time, retry count, throughput | Shows impact on non-real-time traffic. |
| Video stream/probe | Packet loss, jitter, latency, bitrate/frame drops | Shows sensitivity to WAN quality. |

Application health must be recorded with timestamps so the team can prove the prediction happened before service impact.

## Fault injection

Start with one reproducible, gradual fault:

| Fault | Injection point | Expected precursor | Expected recommendation |
|---|---|---|---|
| Gradual packet loss/jitter | Primary branch-to-DC path or tunnel egress | Rising loss, jitter, retransmits, video quality decline risk | Validate backup path and request traffic shift/failover. |

Later scenarios may include congestion, BGP/OSPF flaps, tunnel/rekey failures, and policy drift. Each injected run must record its start, end, location, severity, and seed.

Never inject the fault on an unidentified host interface. The eventual fault script must take an explicit lab node/interface identifier and write a machine-readable fault manifest before changing traffic conditions.

## Minimum acceptance checks

Before recording the demo:

1. The client reaches the server through the primary path.
2. The client can reach the server through the backup path after a controlled switch.
3. Every lab entity has an inventory ID, site, role, interface/path mapping, and topology version.
4. At least one router/tunnel health metric and one application-health metric are visible in the local store.
5. The management plane remains reachable when the primary path is degraded.
6. The injected held-out scenario is not in the model's training or threshold-tuning data.

