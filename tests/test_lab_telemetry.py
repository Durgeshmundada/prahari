from datetime import UTC, datetime

import pytest

from prahari.lab_telemetry import build_path_metric_records, parse_ping_output


def test_parse_ping_output_and_build_contract_records() -> None:
    output = """20 packets transmitted, 18 received, 10% packet loss, time 19000ms
rtt min/avg/max/mdev = 20.100/22.500/25.000/1.250 ms
"""
    measurement = parse_ping_output(output)
    assert measurement.packet_loss_ratio == pytest.approx(0.1)
    records = build_path_metric_records(
        measurement,
        observed_at=datetime(2026, 9, 1, tzinfo=UTC),
        run_id="experiment-001",
        phase="impaired",
    )
    assert {record.metric for record in records} == {
        "packet_loss_ratio",
        "jitter_ms",
    }
    assert records[0].labels["rtt_avg_ms"] == "22.500"
    assert all(record.labels["experiment_phase"] == "impaired" for record in records)


def test_parse_ping_output_accepts_total_loss_without_rtt() -> None:
    measurement = parse_ping_output(
        "5 packets transmitted, 0 received, 100% packet loss, time 4100ms"
    )
    assert measurement.packet_loss_ratio == 1
    assert measurement.average_rtt_ms == 0


def test_parse_ping_output_rejects_inconsistent_counts() -> None:
    with pytest.raises(ValueError, match="does not match"):
        parse_ping_output("10 packets transmitted, 9 received, 50% packet loss")
