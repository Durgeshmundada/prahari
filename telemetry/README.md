# Telemetry

PRAHARI supports normalized JSONL replay and live, durably spooled collection.
The controlled Containerlab path collector measures real ICMP packet loss, RTT,
and jitter. It ingests the model-enabled loss and jitter metrics while retaining
RTT as evidence metadata, and labels every sample with its run and phase. Production
collectors require HTTPS; plain HTTP is an explicit lab-only override.

For forecasting evidence, use the separate
[`predictive-v2` workflow](../docs/predictive-v2.md). It joins real path and
same-path application observations, derives labels from sustained observed
application impact, censors incomplete future horizons, and keeps entire runs in
one train/validation/test/demo-holdout partition.
