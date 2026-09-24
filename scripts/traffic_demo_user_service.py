"""Run PRAHARI's private-LAN browser service for a two-laptop demonstration."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from prahari.traffic_demo.protocol import DEFAULT_MAX_FILE_BYTES, ReceiverLimits
from prahari.traffic_demo.receiver import validate_bind_options
from prahari.traffic_demo.telemetry import TelemetryPublisher
from prahari.traffic_demo.user_service import UserDemoConfig, run_user_demo


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Serve a bounded chat/file page on one private-LAN address and publish only "
            "measurements to the local PRAHARI collector."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host by default.")
    parser.add_argument("--port", type=_port, default=8780)
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly allow a literal RFC1918/ULA address; requires an access code.",
    )
    parser.add_argument(
        "--access-token-env",
        default="PRAHARI_USER_DEMO_TOKEN",
        help="Environment variable containing the browser access code.",
    )
    parser.add_argument("--collector-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--collector-api-key-env",
        default="PRAHARI_COLLECTOR_API_KEY",
        help="Optional environment variable containing the local collector API key.",
    )
    parser.add_argument(
        "--max-file-mib",
        type=_positive_int,
        default=DEFAULT_MAX_FILE_BYTES // (1024 * 1024),
    )
    parser.add_argument(
        "--no-publish",
        action="store_true",
        help="Run the browser workload without saving dashboard evidence.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = os.environ.get(args.access_token_env)
    try:
        token = validate_bind_options(
            args.host,
            allow_non_loopback=args.allow_non_loopback,
            shared_token=token,
        )
        limits = ReceiverLimits(max_file_bytes=args.max_file_mib * 1024 * 1024)
        publisher = None
        if not args.no_publish:
            publisher = TelemetryPublisher(
                args.collector_url,
                api_key=os.environ.get(args.collector_api_key_env),
                actor_id="two-laptop-user-demo",
            )
        config = UserDemoConfig(
            assets_directory=Path(__file__).resolve().parents[1] / "apps" / "user-demo",
            access_token=token,
            publisher=publisher,
            limits=limits,
        )
    except ValueError as error:
        print(f"user-demo configuration error: {error}", file=sys.stderr)
        return 2

    print(
        f"Starting PRAHARI user demo at http://{args.host}:{args.port}; "
        f"file cap {limits.max_file_bytes // (1024 * 1024)} MiB; "
        f"dashboard evidence {'disabled' if publisher is None else 'enabled'}."
    )
    run_user_demo(
        host=args.host,
        port=args.port,
        config=config,
        allow_non_loopback=args.allow_non_loopback,
    )
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _port(value: str) -> int:
    parsed = _positive_int(value)
    if parsed > 65_535:
        raise argparse.ArgumentTypeError("must be between 1 and 65535")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
