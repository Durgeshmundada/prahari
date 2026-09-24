# Containerlab / FRRouting scaffold

> **Status: routed lab validated on WSL2, not production-certified.** On
> 2026-09-01 this topology was deployed on Ubuntu 24.04 under WSL2 with Docker
> Engine 29.7.2, Compose 5.5.0, Containerlab 0.79.0, and FRR 10.2.1. OSPF,
> primary-path selection in both directions, end-to-end ICMP, bounded netem
> impairment, and recovery were verified. This remains an isolated routed
> OSPF lab—not yet an MPLS data plane, SD-WAN overlay, or production network.

`topology.clab.yml` defines an eight-node routed FRR/OSPF data path plus a ninth
PRAHARI API node on the out-of-band Docker management network. It deliberately
models a preferred and a higher-cost backup path so the primary egress can be
degraded in a controlled experiment. The branch and DC endpoints run bounded
chat and verified file traffic across that data path while evidence is published
separately to the management API. It is not yet an MPLS data plane, an SD-WAN
overlay, or a vendor-router telemetry collector.

## Intended topology

```text
                             primary path (lower OSPF cost)
branch-client -- branch-edge ---- pe-primary ---- core ---- pe-secondary ---- dc-edge -- app-server
                    |                                                               |
                    +--------------------- pe-backup ------------------------------+
                              backup path (higher OSPF cost)

All nodes also have eth0 on the separate Containerlab management network.
```

The intended fault point is **`branch-edge:eth1`**. It is the primary-path
egress and is the only interface accepted by the supplied netem script. The
backup path begins at `branch-edge:eth2`, whose OSPF cost is deliberately higher.

| Link / network | Addressing | Purpose |
|---|---|---|
| branch-client ↔ branch-edge | `172.16.10.0/24` | branch user LAN |
| branch-edge ↔ pe-primary | `10.10.0.0/30` | preferred path ingress / fault point |
| pe-primary ↔ core | `10.10.1.0/30` | preferred path |
| core ↔ pe-secondary | `10.10.2.0/30` | preferred path |
| pe-secondary ↔ dc-edge | `10.10.3.0/30` | preferred path egress |
| branch-edge ↔ pe-backup | `10.20.0.0/30` | backup path ingress |
| pe-backup ↔ dc-edge | `10.20.1.0/30` | backup path egress |
| dc-edge ↔ app-server | `172.16.20.0/24` | application LAN |
| Containerlab management | `172.31.255.0/24` | out-of-band management only |

If any of these networks overlap with the selected Linux host or Docker
environment, stop and choose non-overlapping ranges before deployment.

## Linux prerequisites

Run this lab only on an authorized Linux host or Linux VM. Required:

- Docker Engine, in Linux-container mode, reachable by the operator;
- Containerlab with the `validate` command available;
- Bash, `iproute2` (`ip` and `tc`), and `grep` on the host;
- the `sch_netem` kernel module enabled for the controlled fault scenario;
- enough resources for eight privileged containers (start with 4 vCPU, 8 GiB
  RAM, and 20 GiB free disk; size from measured load before a demo); and
- the FRR and network-multitool images preloaded from an approved, pinned image
  source. The topology's example tags must be replaced with verified digests in
  a release manifest.

Containerlab nodes are privileged by default and this scaffold requests that
explicitly because routing and `tc` need network administration capabilities.
Treat lab access like root access. Use an isolated host; never run this against
production interfaces or join its management network to an untrusted network.

## Non-destructive validation

These commands validate only; none deploys, destroys, pulls images, or modifies
host networking:

```bash
# Parses Containerlab schema/nodes/links without deploying the lab.
bash network-lab/scripts/validate-topology.sh

# Checks native Linux, Docker reachability, preloaded images, sch_netem, and then
# invokes the non-deploy topology validation above.
bash network-lab/scripts/preflight.sh

# Preview Containerlab's deployment plan without applying it.
containerlab deploy -t network-lab/topology.clab.yml --dry-run
```

`preflight.sh` intentionally fails rather than trying to install software, pull
images, load a kernel module, or modify permissions. That keeps the operator in
control of host changes.

## Deliberate deployment step

Only after the validation output, image provenance, addressing, and rollback
plan have been reviewed by the lab owner should an authorized operator run:

```bash
containerlab deploy -t network-lab/topology.clab.yml
```

That command creates privileged containers and virtual links. It is intentionally
not hidden behind a helper script. After it completes, the read-only runtime
check can inspect OSPF state and optionally send three ICMP probes:

```bash
bash network-lab/scripts/runtime-check.sh --lab-name prahari
# Omit traffic entirely if needed:
bash network-lab/scripts/runtime-check.sh --lab-name prahari --skip-traffic
```

The runtime check fails closed on missing containers, wrong labels, incomplete
OSPF adjacency, non-primary routes, or packet loss. The separate application
check verifies the running sender/receiver and the latest persisted checksum
receipt:

```bash
bash network-lab/scripts/app-traffic-check.sh
```

These checks still do not prove backup convergence, predictive accuracy, or
production capacity.

## Labelled telemetry experiments

The experiment runner collects real ICMP loss, RTT, and jitter into the durable
collector spool for baseline, impaired, and recovered phases. It automatically
clears its netem fault on normal completion, error, or interruption.

```bash
bash network-lab/scripts/run-labelled-experiment.sh \
  --samples 12 --loss-percent 5 --delay-ms 20 --jitter-ms 5 \
  --confirm RUN-LABELLED-EXPERIMENT
```

Results and machine-readable labels are retained under
`network-lab/state/experiments/<run-id>/`. Keep held-out evaluation run IDs
separate from training data.

## Controlled fault workflow

The injector is intentionally narrow: it rejects all lab names, nodes, and
interfaces except `prahari / branch-edge / eth1`; defaults to dry-run; limits
severity; checks the Containerlab label; refuses an existing netem qdisc; and
writes a local manifest before changing the lab interface.

```bash
# No change; prints exactly what a fault would do.
bash network-lab/scripts/inject-netem.sh \
  --lab-name prahari --node branch-edge --interface eth1 \
  --fault-id gradual-loss-001 --loss-percent 1.0 --delay-ms 20 --jitter-ms 5

# Requires a deliberate, matching acknowledgement to change the isolated lab.
bash network-lab/scripts/inject-netem.sh \
  --lab-name prahari --node branch-edge --interface eth1 \
  --fault-id gradual-loss-001 --loss-percent 1.0 --delay-ms 20 --jitter-ms 5 \
  --apply --confirm-fault-id gradual-loss-001

# Controlled recovery is gated separately and refuses to remove a non-netem qdisc.
bash network-lab/scripts/clear-netem.sh \
  --lab-name prahari --node branch-edge --interface eth1 \
  --apply --confirm-clear-netem CLEAR-NETEM
```

Per-run manifests are written to `network-lab/state/faults/` and intentionally
ignored by version control. Copy them into controlled experiment evidence before
resetting a lab. The scripts do not alter routing policy, trigger failover, or
touch anything outside the specific named lab container.

## Recorded WSL2 validation

The first controlled validation produced:

- two Full OSPF neighbors at the branch edge;
- forward and return routes over the primary path at OSPF metric 50;
- healthy traffic with 0% loss;
- an allow-listed `10% loss / 20 ms delay / 5 ms jitter` netem fault that
  measured 10% loss and approximately 20 ms RTT; and
- explicit qdisc cleanup followed by 0% loss and sub-millisecond lab RTT.

The images in the topology are pinned by registry digest. The fault manifest is
stored under `network-lab/state/faults/` as local experiment evidence. This
validation proves the routed fault harness only; it does not validate prediction
accuracy, MPLS, an SD-WAN controller, or a network action adapter.

## Work required before production-like use

1. Validate exact Containerlab, FRR, Docker, and kernel versions on the target
   Linux platform; pin their image digests and record checksums.
2. Add and test MPLS/LDP/SR policy as appropriate, plus the selected SD-WAN-like
   overlay and controlled, approved backup-path action.
3. Add read-only router telemetry collection (for example gNMI/SNMP/syslog) on the
   management plane; never use faulted-path traffic for its control channel.
4. Test primary degradation, manual backup validation, rollback,
   host reboot recovery, lab destroy/recreate, and evidence retention.
5. Keep held-out fault runs separate from model training and threshold tuning.

Containerlab supports a non-deploy `validate` command and FRR configurations can
be bind-mounted into Linux-kind nodes; this scaffold follows those documented
patterns. The routed workload and fault harness have been validated on WSL2;
prediction accuracy and production capacity remain unverified.

The original baseline/impaired/recovered runner supports the experimental
current-state detector. Genuine 30/60/120-second future-impact experiments use
the gradual-ramp runner and evidence workflow documented in
[`docs/predictive-v2.md`](../docs/predictive-v2.md); fault phase names are not
used as predictive labels.
