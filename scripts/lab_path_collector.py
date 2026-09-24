"""Collect one real lab path sample and durably forward it to PRAHARI."""

from __future__ import annotations

import argparse
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from prahari.collector import SpoolQueue, TelemetryForwarder, http_transport
from prahari.lab_telemetry import build_path_metric_records, parse_ping_output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--phase", choices=("normal", "baseline", "impaired", "recovered"), required=True
    )
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--spool", type=Path, default=Path("/var/lib/prahari/path-spool"))
    parser.add_argument("--api-url", default="http://127.0.0.1:8080")
    parser.add_argument("--api-key-file", type=Path, required=True)
    args = parser.parse_args()
    if not 3 <= args.count <= 100:
        parser.error("--count must be between 3 and 100")

    result = subprocess.run(
        ["ping", "-n", "-q", "-c", str(args.count), "-W", "2", "172.16.20.2"],
        check=False,
        capture_output=True,
        text=True,
        timeout=args.count * 3,
    )
    # ping returns non-zero for partial/total loss; its summary is still valid evidence.
    measurement = parse_ping_output(result.stdout + result.stderr)
    records = build_path_metric_records(
        measurement,
        observed_at=datetime.now(UTC),
        run_id=args.run_id,
        phase=args.phase,
    )
    queue = SpoolQueue(args.spool)
    queue.enqueue("metric", records, run_id=args.run_id)
    token = args.api_key_file.read_text(encoding="utf-8").strip()
    delivered = TelemetryForwarder(
        queue,
        http_transport(args.api_url, token, allow_insecure_http=True),
    ).flush()
    print(
        f"phase={args.phase} sent={measurement.sent} received={measurement.received} "
        f"loss={measurement.packet_loss_ratio:.4f} rtt_ms={measurement.average_rtt_ms:.3f} "
        f"jitter_ms={measurement.jitter_ms:.3f} delivered={delivered}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
