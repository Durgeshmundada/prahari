import os
from pathlib import Path

import pytest

from prahari.collector import CollectorError, SpoolQueue, TelemetryForwarder, http_transport
from prahari.contracts import MetricRecord


def metric() -> MetricRecord:
    return MetricRecord.model_validate(
        {
            "observed_at": "2026-08-20T09:00:00Z",
            "source": "collector-test",
            "entity_id": "tunnel:branch-01-to-dc-primary",
            "entity_type": "tunnel",
            "metric": "packet_loss_ratio",
            "value": 0.01,
            "unit": "ratio",
            "topology_version": "lab-v1",
            "config_version": "lab-config-v1",
            "quality": {"source_healthy": True},
        }
    )


def test_spool_is_durable_deduplicated_and_acknowledged(tmp_path: Path) -> None:
    queue = SpoolQueue(tmp_path / "spool")
    first = queue.enqueue("metric", [metric()], run_id="run-1")
    second = queue.enqueue("metric", [metric()], run_id="run-1")

    assert first.batch_id == second.batch_id
    assert len(queue.pending()) == 1

    calls: list[tuple[str, dict[str, object]]] = []

    def transport(endpoint: str, payload: dict[str, object]) -> int:
        calls.append((endpoint, payload))
        return 1

    assert TelemetryForwarder(queue, transport).flush() == 1
    assert calls[0][0] == "/api/telemetry/metrics"
    assert calls[0][1]["run_id"] == "run-1"
    assert queue.pending() == []


def test_failed_delivery_remains_spooled_for_retry(tmp_path: Path) -> None:
    queue = SpoolQueue(tmp_path / "spool")
    queue.enqueue("metric", [metric()])

    def unavailable(_endpoint: str, _payload: dict[str, object]) -> int:
        raise CollectorError("offline")

    with pytest.raises(CollectorError, match="offline"):
        TelemetryForwarder(queue, unavailable).flush()
    assert len(queue.pending()) == 1


def test_spool_flushes_in_enqueue_time_order_instead_of_hash_order(tmp_path: Path) -> None:
    queue = SpoolQueue(tmp_path / "spool")
    queue.enqueue("metric", [metric()], run_id="first-run")
    changed = metric().model_copy(update={"value": 0.02})
    queue.enqueue("metric", [changed], run_id="second-run")
    paths = sorted(queue.root.glob("*.json"))
    assert len(paths) == 2
    os.utime(paths[0], ns=(2_000_000_000, 2_000_000_000))
    os.utime(paths[1], ns=(1_000_000_000, 1_000_000_000))

    expected = [queue._read(paths[1]).run_id, queue._read(paths[0]).run_id]

    assert [batch.run_id for batch in queue.pending()] == expected


def test_spool_applies_backpressure_at_capacity(tmp_path: Path) -> None:
    queue = SpoolQueue(tmp_path / "spool", max_items=1)
    queue.enqueue("metric", [metric()])
    changed = metric().model_copy(update={"value": 0.02})

    with pytest.raises(CollectorError, match="spool is full"):
        queue.enqueue("metric", [changed])


def test_acknowledgement_mismatch_does_not_delete_evidence(tmp_path: Path) -> None:
    queue = SpoolQueue(tmp_path / "spool")
    queue.enqueue("metric", [metric()])

    with pytest.raises(CollectorError, match="acknowledgement mismatch"):
        TelemetryForwarder(queue, lambda _endpoint, _payload: 0).flush()
    assert len(queue.pending()) == 1


def test_remote_plain_http_requires_explicit_lab_override() -> None:
    with pytest.raises(CollectorError, match="must use HTTPS"):
        http_transport("http://172.31.255.18:8080", "token")
    assert callable(
        http_transport("http://172.31.255.18:8080", "token", allow_insecure_http=True)
    )
