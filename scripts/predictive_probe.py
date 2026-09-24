"""Capture one joined, real network/application observation for predictive-v2."""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from prahari.analytics.predictive import (
    ApplicationMeasurement,
    InjectedCondition,
    NetworkMeasurement,
    PredictiveObservation,
)
from prahari.collector import SpoolQueue, TelemetryForwarder, http_transport
from prahari.lab_telemetry import build_path_metric_records, parse_ping_output
from prahari.traffic_demo.sender import TrafficProbeConfig, run_traffic_demo
from prahari.traffic_demo.telemetry import TelemetryPublisher

PREDICTIVE_APP_TIMEOUT_SECONDS = 0.5


def _bounded_error(value: object | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:500] or None


def _phase_for_metric_label(phase: str) -> str:
    return {
        "baseline": "baseline",
        "ramp": "impaired",
        "steady": "impaired",
        "recovery": "recovered",
    }[phase]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lab-name", choices=("prahari", "prahari-owned"), default="prahari")
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--scenario", choices=("normal", "gradual_loss"), required=True)
    parser.add_argument(
        "--phase", choices=("baseline", "ramp", "steady", "recovery"), required=True
    )
    parser.add_argument("--loss-percent", type=float, required=True)
    parser.add_argument("--delay-ms", type=float, required=True)
    parser.add_argument("--jitter-ms", type=float, required=True)
    parser.add_argument("--ping-count", type=int, default=3)
    parser.add_argument("--receiver-url", default="ws://172.16.20.2:8765/ws/traffic")
    parser.add_argument("--api-url", default="http://172.31.255.18:8080")
    parser.add_argument("--api-key-file", type=Path, default=Path("/run/prahari-lab-api-key"))
    parser.add_argument("--spool", type=Path, default=Path("/var/lib/prahari/path-spool"))
    parser.add_argument("--chat-messages", type=int, default=5)
    parser.add_argument("--file-kib", type=int, default=4)
    args = parser.parse_args()
    if not 3 <= args.ping_count <= 20:
        parser.error("--ping-count must be 3..20")
    if not 3 <= args.chat_messages <= 20:
        parser.error("--chat-messages must be 3..20")
    if not 1 <= args.file_kib <= 128:
        parser.error("--file-kib must be 1..128")
    injected = InjectedCondition(
        loss_percent=args.loss_percent,
        delay_ms=args.delay_ms,
        jitter_ms=args.jitter_ms,
    )
    token = os.environ.get("PRAHARI_TRAFFIC_DEMO_TOKEN", "")
    if not token:
        parser.error("PRAHARI_TRAFFIC_DEMO_TOKEN is required")
    api_key = args.api_key_file.read_text(encoding="utf-8").strip()

    probe_started_at = datetime.now(UTC)
    ping = subprocess.run(
        ["ping", "-n", "-q", "-c", str(args.ping_count), "-W", "2", "172.16.20.2"],
        check=False,
        capture_output=True,
        text=True,
        timeout=args.ping_count * 3,
    )
    network_error: str | None = None
    network_persisted = False
    try:
        measurement = parse_ping_output(ping.stdout + ping.stderr)
        network_at = datetime.now(UTC)
        records = build_path_metric_records(
            measurement,
            observed_at=network_at,
            run_id=args.run_id,
            phase=_phase_for_metric_label(args.phase),
        )
        queue = SpoolQueue(args.spool)
        batch = queue.enqueue("metric", records, run_id=args.run_id)
        TelemetryForwarder(
            queue,
            http_transport(args.api_url, api_key, allow_insecure_http=True),
        ).flush()
        # A delivered count can refer to an older queued batch.  The current
        # observation is eligible only when its exact content-addressed spool
        # entry was acknowledged and removed.
        network_persisted = not (queue.root / f"{batch.batch_id}.json").exists()
    except (OSError, ValueError, RuntimeError) as error:
        network_error = _bounded_error(error)
        raise RuntimeError(
            f"network probe did not produce valid evidence: {network_error}"
        ) from error

    config = TrafficProbeConfig(
        receiver_url=args.receiver_url,
        collector_url=args.api_url,
        shared_token=token,
        allow_non_loopback_receiver=True,
        allow_insecure_remote_ws=True,
        chat_messages=args.chat_messages,
        generated_file_bytes=args.file_kib * 1024,
        max_file_bytes=128 * 1024,
        chunk_bytes=min(args.file_kib * 1024, 64 * 1024),
        # Chat messages are measured sequentially.  The normal traffic-demo
        # default (five seconds per missing echo) can exceed this experiment's
        # ten-second network/application join ceiling under intentional loss.
        # The lab path's maximum configured RTT is below this frozen bound,
        # while nine missed heavy-profile messages plus the final drain and
        # transfer acknowledgements still remain below the join ceiling.
        timeout_seconds=PREDICTIVE_APP_TIMEOUT_SECONDS,
        run_id=args.run_id,
    )
    publisher = TelemetryPublisher(
        args.api_url,
        api_key=api_key,
        allow_insecure_private_http=True,
    )
    result = asyncio.run(run_traffic_demo(config, publisher=publisher))
    app_passed = (
        result.chat.received == result.chat.attempted
        and result.upload.status == "completed"
        and result.upload.integrity == "verified"
    )
    application_error = _bounded_error(
        result.run_error or result.upload.error or result.collector_error
    )
    observation = PredictiveObservation(
        run_id=args.run_id,
        sequence=args.sequence,
        controller_lab_name=args.lab_name,
        scenario=args.scenario,
        phase=args.phase,
        target_entity_id="interface:branch-edge:eth1",
        injected=injected,
        observed_at=result.finished_at,
        probe_started_at=probe_started_at,
        network_observed_at=network_at,
        application_observed_at=result.finished_at,
        topology_version="lab-v1",
        config_version="lab-config-v1",
        local_evidence_complete=True,
        network_records_persisted=network_persisted,
        application_records_persisted=result.evidence_saved,
        network_error=network_error,
        application_error=application_error,
        network=NetworkMeasurement(
            sent=measurement.sent,
            received=measurement.received,
            packet_loss_ratio=measurement.packet_loss_ratio,
            rtt_avg_ms=measurement.average_rtt_ms,
            jitter_ms=measurement.jitter_ms,
        ),
        application=ApplicationMeasurement(
            status="passed" if app_passed else "failed",
            messages_attempted=result.chat.attempted,
            messages_received=result.chat.received,
            message_loss_ratio=result.chat.packet_loss_ratio,
            rtt_p95_ms=result.chat.p95_rtt_ms,
            jitter_ms=result.chat.jitter_ms,
            upload_status=result.upload.status,
            upload_integrity=result.upload.integrity,
            upload_throughput_mbps=result.upload.throughput_mbps,
            evidence_saved=result.evidence_saved,
        ),
    )
    print(observation.model_dump_json(), flush=True)
    return 0 if network_persisted and result.evidence_saved else 3


if __name__ == "__main__":
    raise SystemExit(main())
