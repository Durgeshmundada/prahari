"""Durable, bounded forwarding for vendor telemetry adapters.

Adapters normalize source data into PRAHARI contracts, enqueue a batch first,
then forward it. The deterministic evidence store makes retries idempotent.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import TypeAdapter

from prahari.contracts import AppProbeRecord, EventRecord, MetricRecord

RecordKind = Literal["metric", "app_probe", "event"]
Record = MetricRecord | AppProbeRecord | EventRecord
_RECORD_ADAPTERS = {
    "metric": TypeAdapter(list[MetricRecord]),
    "app_probe": TypeAdapter(list[AppProbeRecord]),
    "event": TypeAdapter(list[EventRecord]),
}
_ENDPOINTS = {
    "metric": "/api/telemetry/metrics",
    "app_probe": "/api/telemetry/app-probes",
    "event": "/api/telemetry/events",
}


class CollectorError(RuntimeError):
    """A safe, operator-visible collector failure."""


@dataclass(frozen=True)
class SpoolBatch:
    batch_id: str
    record_type: RecordKind
    run_id: str | None
    records: list[Record]

    def payload(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "records": [record.model_dump(mode="json") for record in self.records],
        }


class SpoolQueue:
    """Crash-safe FIFO queue with explicit disk and item bounds."""

    def __init__(self, root: Path, *, max_items: int = 10_000, max_bytes: int = 256_000_000):
        if max_items < 1 or max_bytes < 1:
            raise ValueError("spool limits must be positive")
        self.root = root.resolve()
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def enqueue(
        self,
        record_type: RecordKind,
        records: list[Record],
        *,
        run_id: str | None = None,
    ) -> SpoolBatch:
        if not records:
            raise CollectorError("cannot enqueue an empty telemetry batch")
        validated = _validate_records(record_type, records)
        body = {
            "record_type": record_type,
            "run_id": run_id,
            "records": [record.model_dump(mode="json") for record in validated],
        }
        encoded_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        batch_id = sha256(encoded_body).hexdigest()
        envelope = json.dumps(
            {"batch_id": batch_id, **body}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        target = self.root / f"{batch_id}.json"
        if target.exists():
            return self._read(target)
        self._require_capacity(len(envelope))
        with NamedTemporaryFile(
            dir=self.root,
            prefix=".pending-",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            temporary = Path(tmp.name)
            tmp.write(envelope)
            tmp.flush()
            os.fsync(tmp.fileno())
        try:
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return SpoolBatch(batch_id, record_type, run_id, list(validated))

    def pending(self) -> list[SpoolBatch]:
        paths = self.root.glob("[0-9a-f]*.json")
        ordered = sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))
        return [self._read(path) for path in ordered]

    def acknowledge(self, batch_id: str) -> None:
        if len(batch_id) != 64 or any(char not in "0123456789abcdef" for char in batch_id):
            raise CollectorError("invalid spool batch identifier")
        (self.root / f"{batch_id}.json").unlink(missing_ok=True)

    def _require_capacity(self, incoming_bytes: int) -> None:
        files = list(self.root.glob("[0-9a-f]*.json"))
        used_bytes = sum(path.stat().st_size for path in files)
        if len(files) >= self.max_items or used_bytes + incoming_bytes > self.max_bytes:
            raise CollectorError(
                "telemetry spool is full; source collection must apply backpressure"
            )

    @staticmethod
    def _read(path: Path) -> SpoolBatch:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            record_type: RecordKind = raw["record_type"]
            records = _validate_records(record_type, raw["records"])
            batch_id = str(raw["batch_id"])
            run_id = raw.get("run_id")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise CollectorError(f"invalid telemetry spool entry: {path.name}") from error
        if path.stem != batch_id:
            raise CollectorError(f"telemetry spool identity mismatch: {path.name}")
        return SpoolBatch(batch_id, record_type, run_id, list(records))


Transport = Callable[[str, dict[str, object]], int]


class TelemetryForwarder:
    def __init__(self, queue: SpoolQueue, transport: Transport):
        self.queue = queue
        self.transport = transport

    def flush(self, *, limit: int = 100) -> int:
        if not 1 <= limit <= 10_000:
            raise ValueError("flush limit must be between 1 and 10000")
        delivered = 0
        for batch in self.queue.pending()[:limit]:
            accepted = self.transport(_ENDPOINTS[batch.record_type], batch.payload())
            if accepted != len(batch.records):
                raise CollectorError(
                    f"collector acknowledgement mismatch for {batch.batch_id}: "
                    f"expected {len(batch.records)}, accepted {accepted}"
                )
            self.queue.acknowledge(batch.batch_id)
            delivered += len(batch.records)
        return delivered


def http_transport(
    base_url: str,
    api_key: str,
    *,
    timeout_seconds: float = 10.0,
    allow_insecure_http: bool = False,
) -> Transport:
    base = base_url.rstrip("/")
    is_local = base.startswith(("http://127.0.0.1", "http://localhost"))
    if not (
        base.startswith("https://")
        or is_local
        or (allow_insecure_http and base.startswith("http://"))
    ):
        raise CollectorError("remote collector targets must use HTTPS")
    if not api_key.strip():
        raise CollectorError("collector API key is required")

    def send(endpoint: str, payload: dict[str, object]) -> int:
        request = Request(
            f"{base}{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                # Used only when a local development API has authentication
                # disabled. API-key mode ignores this untrusted identity header.
                "X-Actor-Role": "collector",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "prahari-collector/1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                body = json.load(response)
        except HTTPError as error:
            raise CollectorError(f"PRAHARI rejected telemetry with HTTP {error.code}") from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise CollectorError("PRAHARI telemetry endpoint is unavailable") from error
        accepted = body.get("accepted") if isinstance(body, dict) else None
        if not isinstance(accepted, int):
            raise CollectorError("PRAHARI returned an invalid telemetry acknowledgement")
        return accepted

    return send


def _validate_records(record_type: RecordKind, records: object) -> list[Record]:
    try:
        adapter = _RECORD_ADAPTERS[record_type]
    except KeyError as error:
        raise CollectorError(f"unsupported telemetry record type: {record_type}") from error
    return list(adapter.validate_python(records))
