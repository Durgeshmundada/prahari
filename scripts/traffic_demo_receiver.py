"""Run the local/DC side of PRAHARI's bounded WebSocket traffic demo."""

from __future__ import annotations

import argparse
import os
import sys

from prahari.traffic_demo.protocol import DEFAULT_MAX_CHUNK_BYTES, DEFAULT_MAX_FILE_BYTES
from prahari.traffic_demo.receiver import (
    ReceiverConfig,
    ReceiverLimits,
    run_receiver,
    validate_bind_options,
)
from prahari.traffic_demo.storage import ReceiverArtifactStore, ReceiverArtifactStoreConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the bounded PRAHARI traffic-demo receiver. It hashes uploads in memory and can "
            "retain only verified bytes in a short-lived, process-local artifact cache."
        )
    )
    parser.add_argument("--host", default="127.0.0.1", help="Loopback host by default.")
    parser.add_argument("--port", type=_port, default=8765)
    parser.add_argument(
        "--max-file-mib",
        type=_positive_int,
        default=DEFAULT_MAX_FILE_BYTES // (1024 * 1024),
        help="Per-upload cap; hard-capped at 20 MiB.",
    )
    parser.add_argument(
        "--chunk-kib",
        type=_positive_int,
        default=DEFAULT_MAX_CHUNK_BYTES // 1024,
        help="Maximum WebSocket binary frame; default 64 KiB.",
    )
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly allow only a private-LAN RFC1918/ULA host with a shared token.",
    )
    parser.add_argument(
        "--shared-token-env",
        default="PRAHARI_TRAFFIC_DEMO_TOKEN",
        help=(
            "Environment variable containing the shared token; the token is never accepted "
            "as CLI text."
        ),
    )
    parser.add_argument(
        "--retain-verified-artifacts",
        action="store_true",
        help="Retain verified receiver bytes in bounded memory for temporary API download.",
    )
    parser.add_argument(
        "--artifact-token-env",
        default="PRAHARI_TRAFFIC_DEMO_ARTIFACT_TOKEN",
        help="Environment variable containing the separate receiver artifact-service token.",
    )
    parser.add_argument("--artifact-total-mib", type=_positive_int, default=25)
    parser.add_argument("--artifact-max-items", type=_positive_int, default=10)
    parser.add_argument("--artifact-ttl-seconds", type=_positive_int, default=600)
    parser.add_argument("--artifact-download-concurrency", type=_positive_int, default=2)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    token = os.environ.get(args.shared_token_env)
    artifact_token = os.environ.get(args.artifact_token_env)
    try:
        token = validate_bind_options(
            args.host,
            allow_non_loopback=args.allow_non_loopback,
            shared_token=token,
        )
        limits = ReceiverLimits(
            max_file_bytes=args.max_file_mib * 1024 * 1024,
            max_chunk_bytes=args.chunk_kib * 1024,
        )
        artifact_store = (
            ReceiverArtifactStore(
                ReceiverArtifactStoreConfig(
                    max_file_bytes=limits.max_file_bytes,
                    max_total_bytes=args.artifact_total_mib * 1024 * 1024,
                    max_items=args.artifact_max_items,
                    ttl_seconds=args.artifact_ttl_seconds,
                )
            )
            if args.retain_verified_artifacts
            else None
        )
    except ValueError as error:
        print(f"traffic-demo receiver configuration error: {error}", file=sys.stderr)
        return 2
    print(
        f"Starting bounded traffic-demo receiver on {args.host}:{args.port}; "
        f"file cap {limits.max_file_bytes // (1024 * 1024)} MiB; "
        f"verified artifact retention {'enabled' if artifact_store is not None else 'disabled'}."
    )
    run_receiver(
        host=args.host,
        port=args.port,
        config=ReceiverConfig(
            limits=limits,
            shared_token=token,
            artifact_store=artifact_store,
            artifact_service_token=artifact_token,
            artifact_download_concurrency=args.artifact_download_concurrency,
        ),
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
