"""Validate, durably spool, and forward normalized PRAHARI telemetry JSONL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast

from prahari.collector import (
    CollectorError,
    Record,
    RecordKind,
    SpoolQueue,
    TelemetryForwarder,
    http_transport,
)


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="Durably forward normalized telemetry to PRAHARI."
    )
    command.add_argument("--spool", type=Path, required=True)
    command.add_argument("--input", type=Path, help="Normalized JSONL to enqueue before flushing")
    command.add_argument("--run-id")
    command.add_argument("--api-url", required=True)
    command.add_argument(
        "--api-key-file",
        type=Path,
        required=True,
        help="Path to a mounted collector API-key secret",
    )
    command.add_argument("--batch-size", type=int, default=250)
    command.add_argument("--flush-limit", type=int, default=100)
    return command


def main() -> int:
    args = parser().parse_args()
    if not 1 <= args.batch_size <= 5_000:
        raise SystemExit("--batch-size must be between 1 and 5000")
    try:
        queue = SpoolQueue(args.spool)
        if args.input is not None:
            enqueue_jsonl(queue, args.input, run_id=args.run_id, batch_size=args.batch_size)
        token = args.api_key_file.read_text(encoding="utf-8").strip()
        forwarder = TelemetryForwarder(queue, http_transport(args.api_url, token))
        delivered = forwarder.flush(limit=args.flush_limit)
    except (CollectorError, OSError) as error:
        print(f"collector stopped safely: {error}")
        return 1
    print(f"collector delivered {delivered} records; {len(queue.pending())} batches remain")
    return 0


def enqueue_jsonl(
    queue: SpoolQueue,
    input_path: Path,
    *,
    run_id: str | None,
    batch_size: int,
) -> int:
    grouped: dict[RecordKind, list[Record]] = {"metric": [], "app_probe": [], "event": []}
    count = 0
    with input_path.open("r", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                record_type = cast(RecordKind, raw["record_type"])
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise CollectorError(f"invalid JSONL record at line {line_number}") from error
            try:
                bucket = grouped[record_type]
            except KeyError as error:
                raise CollectorError(
                    f"unsupported record_type at line {line_number}"
                ) from error
            bucket.append(raw)
            count += 1
            if len(bucket) >= batch_size:
                queue.enqueue(record_type, bucket, run_id=run_id)
                bucket.clear()
    for record_type, records in grouped.items():
        if records:
            queue.enqueue(record_type, records, run_id=run_id)
    return count


if __name__ == "__main__":
    raise SystemExit(main())
