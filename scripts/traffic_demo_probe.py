"""Run the branch side of PRAHARI's bounded application-traffic demo."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from prahari.traffic_demo.protocol import DEFAULT_MAX_CHUNK_BYTES, DEFAULT_MAX_FILE_BYTES
from prahari.traffic_demo.sender import (
    TrafficDemoResult,
    TrafficProbeConfig,
    run_periodic_traffic_demo,
)
from prahari.traffic_demo.telemetry import TelemetryPublisher


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Send bounded local-demo chat and a binary file to the receiver, then publish "
            "application RTT/message-loss/checksum evidence to PRAHARI."
        )
    )
    parser.add_argument("--receiver-url", default="ws://127.0.0.1:8765/ws/traffic")
    parser.add_argument("--collector-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--collector-api-key-env",
        default="PRAHARI_COLLECTOR_API_KEY",
        help=(
            "Environment variable for a collector Bearer token; the token is never accepted "
            "as CLI text."
        ),
    )
    parser.add_argument(
        "--shared-token-env",
        default="PRAHARI_TRAFFIC_DEMO_TOKEN",
        help=(
            "Environment variable for a private-LAN receiver token; never pass the token as "
            "CLI text."
        ),
    )
    parser.add_argument("--allow-non-loopback-receiver", action="store_true")
    parser.add_argument(
        "--allow-insecure-remote-ws",
        action="store_true",
        help="Required for ws:// on an isolated private lab only; otherwise use wss://.",
    )
    parser.add_argument(
        "--allow-insecure-private-collector-http",
        action="store_true",
        help="Allow API-key http:// collector only on an isolated private LAN; otherwise use https://.",
    )
    parser.add_argument("--chat-messages", type=_positive_int, default=5)
    parser.add_argument(
        "--file",
        type=Path,
        help="Optional photo/video/file under the bounded cap.",
    )
    parser.add_argument(
        "--generate-file-kib",
        type=_positive_int,
        default=128,
        help="Deterministic generated binary payload when --file is omitted.",
    )
    parser.add_argument(
        "--max-file-mib",
        type=_positive_int,
        default=DEFAULT_MAX_FILE_BYTES // (1024 * 1024),
        help="Sender cap; hard-capped at 20 MiB.",
    )
    parser.add_argument("--chunk-kib", type=_positive_int, default=DEFAULT_MAX_CHUNK_BYTES // 1024)
    parser.add_argument(
        "--repeat",
        type=_non_negative_int,
        default=12,
        help="Runs at 5-second cadence. Use 1 for one run; 0 repeats until Ctrl+C.",
    )
    parser.add_argument(
        "--cadence-seconds",
        type=float,
        default=5.0,
        help="1-10 seconds; 5 seconds keeps live app-probe evidence fresh.",
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Run traffic without collector posting (useful only for receiver troubleshooting).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    status = {"count": 0, "has_failure": False}

    def report(result: TrafficDemoResult) -> None:
        print(json.dumps(result.summary(), indent=2, sort_keys=True), flush=True)
        status["count"] += 1
        status["has_failure"] = status["has_failure"] or not result.passed

    try:
        config = TrafficProbeConfig(
            receiver_url=args.receiver_url,
            collector_url=None if args.no_publish else args.collector_url,
            shared_token=os.environ.get(args.shared_token_env),
            allow_non_loopback_receiver=args.allow_non_loopback_receiver,
            allow_insecure_remote_ws=args.allow_insecure_remote_ws,
            chat_messages=args.chat_messages,
            generated_file_bytes=args.generate_file_kib * 1024,
            sample_file=args.file,
            max_file_bytes=args.max_file_mib * 1024 * 1024,
            chunk_bytes=args.chunk_kib * 1024,
        )
        publisher = (
            None
            if args.no_publish
            else TelemetryPublisher(
                args.collector_url,
                api_key=os.environ.get(args.collector_api_key_env),
                allow_insecure_private_http=args.allow_insecure_private_collector_http,
            )
        )
        asyncio.run(
            run_periodic_traffic_demo(
                config,
                repetitions=None if args.repeat == 0 else args.repeat,
                cadence_seconds=args.cadence_seconds,
                publisher=publisher,
                on_result=report,
                retain_results=False,
            )
        )
    except KeyboardInterrupt:
        print("Traffic-demo probe stopped by operator.", file=sys.stderr)
        return 130
    except ValueError as error:
        print(f"traffic-demo probe configuration error: {error}", file=sys.stderr)
        return 2

    return 0 if status["count"] and not status["has_failure"] else 1


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or a positive integer")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
