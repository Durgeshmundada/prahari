"""Strict parsers and contract mapping for controlled Linux lab path probes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from prahari.contracts import MetricRecord, RecordQuality

_LOSS = re.compile(
    r"(?P<sent>\d+) packets transmitted, (?P<received>\d+) received.*?"
    r"(?P<loss>[\d.]+)% packet loss"
)
_RTT = re.compile(
    r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
    r"(?P<minimum>[\d.]+)/(?P<average>[\d.]+)/(?P<maximum>[\d.]+)/(?P<jitter>[\d.]+) ms"
)


@dataclass(frozen=True)
class PingMeasurement:
    sent: int
    received: int
    packet_loss_ratio: float
    average_rtt_ms: float
    jitter_ms: float


def parse_ping_output(output: str) -> PingMeasurement:
    """Parse iputils ping summary output; reject incomplete or impossible results."""
    loss_match = _LOSS.search(output)
    if loss_match is None:
        raise ValueError("ping output does not contain a packet-loss summary")
    sent = int(loss_match.group("sent"))
    received = int(loss_match.group("received"))
    if sent < 1 or not 0 <= received <= sent:
        raise ValueError("ping output contains impossible packet counts")
    loss = float(loss_match.group("loss")) / 100
    computed_loss = (sent - received) / sent
    if abs(loss - computed_loss) > 0.011:
        raise ValueError("ping packet-loss percentage does not match packet counts")

    rtt_match = _RTT.search(output)
    if rtt_match is None:
        if received == 0:
            return PingMeasurement(sent, received, 1.0, 0.0, 0.0)
        raise ValueError("ping output does not contain an RTT summary")
    average = float(rtt_match.group("average"))
    jitter = float(rtt_match.group("jitter"))
    return PingMeasurement(sent, received, computed_loss, average, jitter)


def build_path_metric_records(
    measurement: PingMeasurement,
    *,
    observed_at: datetime,
    run_id: str,
    phase: str,
) -> list[MetricRecord]:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must include a UTC offset")
    labels = {
        "site_id": "site:branch-01",
        "path_id": "path:branch-01-to-dc:primary",
        "experiment_run_id": run_id,
        "experiment_phase": phase,
        "rtt_avg_ms": f"{measurement.average_rtt_ms:.3f}",
    }
    common = {
        "observed_at": observed_at.astimezone(UTC),
        "source": "containerlab-icmp",
        "entity_id": "tunnel:branch-01-to-dc-primary",
        "entity_type": "path",
        "labels": labels,
        "topology_version": "lab-v1",
        "config_version": "lab-config-v1",
        "quality": RecordQuality(source_healthy=True),
    }
    return [
        MetricRecord(
            metric="packet_loss_ratio",
            value=measurement.packet_loss_ratio,
            unit="ratio",
            **common,
        ),
        MetricRecord(metric="jitter_ms", value=measurement.jitter_ms, unit="ms", **common),
    ]
