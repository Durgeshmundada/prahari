"""Collector payload construction for the local WebSocket traffic demo."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

import httpx

from prahari.contracts import AppProbeRecord, EventRecord
from prahari.traffic_demo.protocol import (
    ProbeMeasurements,
    TrafficDemoError,
    UploadMeasurement,
    is_loopback_host,
    is_private_lan_host,
)

if TYPE_CHECKING:
    from prahari.services.demo import PrahariService

PRIMARY_PATH_ID = "path:branch-01-to-dc:primary"
TOPOLOGY_VERSION = "lab-v1"
CONFIG_VERSION = "lab-config-v1"
CHAT_ENTITY_ID = "app:chat:branch-01-to-dc"
FILE_UPLOAD_ENTITY_ID = "app:file-upload:branch-01-to-dc"


@dataclass(frozen=True)
class TrafficTelemetryContext:
    """Fixed topology metadata required by PRAHARI's normalized ingest contracts."""

    run_id: str
    path_id: str = PRIMARY_PATH_ID
    topology_version: str = TOPOLOGY_VERSION
    config_version: str = CONFIG_VERSION
    source: str = "traffic-demo-branch"


@dataclass(frozen=True)
class CollectorPublishResult:
    """Accepted counts reported by the existing collector API."""

    app_probes_accepted: int
    events_accepted: int


class CollectorPublishError(RuntimeError):
    """Collector was unreachable or rejected the bounded demo evidence."""


class TrafficTelemetryPublisher(Protocol):
    """Small publisher boundary shared by HTTP and in-process evidence sinks."""

    def publish(
        self,
        *,
        app_probes: list[dict[str, Any]],
        events: list[dict[str, Any]],
        run_id: str,
    ) -> Awaitable[CollectorPublishResult]: ...


class TelemetryPublisher:
    """Post only normalized app-probe and event evidence to PRAHARI's collector API."""

    def __init__(
        self,
        collector_url: str,
        *,
        api_key: str | None = None,
        actor_id: str = "traffic-demo-branch",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_insecure_private_http: bool = False,
    ) -> None:
        parsed = urlsplit(collector_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise TrafficDemoError("collector URL must use http:// or https:// and include a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TrafficDemoError("collector URL cannot include credentials, query, or fragment")
        if api_key and not is_loopback_host(parsed.hostname or "") and parsed.scheme != "https":
            if not allow_insecure_private_http or not is_private_lan_host(parsed.hostname or ""):
                raise TrafficDemoError(
                    "an API-key collector must use https://; http:// is allowed only with explicit "
                    "private-LAN acknowledgement for an isolated lab"
                )
        if timeout_seconds <= 0:
            raise TrafficDemoError("collector timeout must be positive")
        self._collector_url = collector_url.rstrip("/")
        self._api_key = api_key
        self._actor_id = actor_id
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def publish(
        self,
        *,
        app_probes: list[dict[str, Any]],
        events: list[dict[str, Any]],
        run_id: str,
    ) -> CollectorPublishResult:
        """Post each record group using the existing role-gated collector endpoints."""

        headers = self._headers()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                event_response = await client.post(
                    f"{self._collector_url}/api/telemetry/events",
                    headers=headers,
                    json={"run_id": run_id, "records": events},
                )
                event_response.raise_for_status()
                app_probe_response = await client.post(
                    f"{self._collector_url}/api/telemetry/app-probes",
                    headers=headers,
                    json={"run_id": run_id, "records": app_probes},
                )
                app_probe_response.raise_for_status()
        except httpx.HTTPError as error:
            raise CollectorPublishError(f"collector publish failed: {error}") from error
        return CollectorPublishResult(
            app_probes_accepted=_accepted_count(app_probe_response),
            events_accepted=_accepted_count(event_response),
        )

    def _headers(self) -> dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {
            "X-Actor-Id": self._actor_id,
            "X-Actor-Role": "collector",
        }


class InProcessTelemetryPublisher:
    """Validate and durably store one exact traffic-demo evidence bundle."""

    def __init__(self, service: PrahariService) -> None:
        self._service = service

    async def publish(
        self,
        *,
        app_probes: list[dict[str, Any]],
        events: list[dict[str, Any]],
        run_id: str,
    ) -> CollectorPublishResult:
        if len(events) != 2 or len(app_probes) != 2:
            raise CollectorPublishError(
                "a traffic-demo run must produce exactly two events and two app probes"
            )
        try:
            # Validate the complete bundle before the first durable write. Events
            # are stored first so a visible transfer probe never precedes its
            # lifecycle evidence.
            event_records = [EventRecord.model_validate(record) for record in events]
            app_probe_records = [
                AppProbeRecord.model_validate(record) for record in app_probes
            ]
            events_accepted = self._service.ingest_events(event_records, run_id=run_id)
            app_probes_accepted = self._service.ingest_app_probes(
                app_probe_records,
                run_id=run_id,
            )
        except Exception as error:
            raise CollectorPublishError("in-process traffic evidence could not be saved") from error
        if events_accepted != 2 or app_probes_accepted != 2:
            raise CollectorPublishError(
                "in-process traffic evidence was not accepted as an exact two-plus-two bundle"
            )
        return CollectorPublishResult(
            app_probes_accepted=app_probes_accepted,
            events_accepted=events_accepted,
        )


def build_app_probe_records(
    *,
    context: TrafficTelemetryContext,
    observed_at: datetime,
    chat: ProbeMeasurements,
    upload: UploadMeasurement,
    kind: str,
) -> list[dict[str, Any]]:
    """Map real application measurements to PRAHARI's chat/file app-probe entities."""

    _require_utc(observed_at)
    if kind not in {"photo", "video", "file"}:
        raise TrafficDemoError("transfer kind must be photo, video, or file")
    timestamp = _timestamp(observed_at)
    common = {
        "schema_version": "1.0",
        "record_type": "app_probe",
        "observed_at": timestamp,
        "path_id": context.path_id,
        "topology_version": context.topology_version,
        "config_version": context.config_version,
        "quality": {
            # Source health means the controlled probe can report an observation;
            # application loss/RTT remains a measurement, not a collector outage.
            "source_healthy": True,
            "late_by_ms": 0,
            "is_estimated": False,
            "counter_reset": False,
        },
    }
    return [
        {
            **common,
            "entity_id": CHAT_ENTITY_ID,
            "probe_name": "chat",
            "packet_loss_ratio": chat.packet_loss_ratio,
            "jitter_ms": chat.jitter_ms,
            "request_latency_ms": chat.p95_rtt_ms,
        },
        {
            **common,
            "entity_id": FILE_UPLOAD_ENTITY_ID,
            "probe_name": "file-upload",
            # A file transfer does not reveal raw packet loss or one-way latency.
            # Keep required legacy fields neutral; chat_measurements below is the
            # explicit source for application RTT/jitter/message-loss evidence.
            "packet_loss_ratio": 0.0,
            "jitter_ms": 0.0,
            "request_latency_ms": 0.0,
            "quality": {
                "source_healthy": True,
                "late_by_ms": 0,
                "is_estimated": False,
                "counter_reset": False,
            },
            "transfer": {
                "transfer_id": upload.transfer_id,
                "direction": "branch_to_dc",
                "protocol": "websocket",
                "content_type": _content_type(upload.file_name),
                "file_name": upload.file_name,
                "kind": kind,
                "bytes_expected": upload.bytes_expected,
                "bytes_transferred": upload.bytes_transferred,
                "bytes_received": upload.bytes_received,
                "status": upload.status,
                "checksum_sha256": upload.checksum_sha256,
                "integrity": upload.integrity,
                "error": upload.error,
                "elapsed_ms": upload.elapsed_seconds * 1_000,
                "throughput_mbps": upload.throughput_mbps,
                "chat_measurements": {
                    "messages_attempted": chat.attempted,
                    "messages_received": chat.received,
                    "rtt_min_ms": chat.min_rtt_ms,
                    "rtt_p50_ms": chat.p50_rtt_ms,
                    "rtt_p95_ms": chat.p95_rtt_ms,
                    "rtt_max_ms": chat.max_rtt_ms,
                    "jitter_ms": chat.jitter_ms,
                },
            },
        },
    ]


def build_event_records(
    *,
    context: TrafficTelemetryContext,
    started_at: datetime,
    finished_at: datetime,
    chat: ProbeMeasurements,
    upload: UploadMeasurement,
) -> list[dict[str, Any]]:
    """Create compact lifecycle events without copying chat text or file content into NOC data."""

    _require_utc(started_at)
    _require_utc(finished_at)
    completed = (
        chat.received == chat.attempted
        and upload.status == "completed"
        and upload.integrity == "verified"
    )
    severity = "info" if completed else "warning"
    final_type = "traffic_demo_completed" if completed else "traffic_demo_degraded"
    common = {
        "schema_version": "1.0",
        "record_type": "event",
        "source": context.source,
        "entity_id": context.path_id,
        "related_entity_ids": [CHAT_ENTITY_ID, FILE_UPLOAD_ENTITY_ID],
        "topology_version": context.topology_version,
        "config_version": context.config_version,
        "quality": {
            "source_healthy": completed,
            "late_by_ms": 0,
            "is_estimated": False,
            "counter_reset": False,
        },
    }
    return [
        {
            **common,
            "observed_at": _timestamp(started_at),
            "event_type": "traffic_demo_started",
            "severity": "info",
            "message": (
                "Local traffic demo started; chat and bounded file transfer are being measured."
            ),
            "quality": {
                "source_healthy": True,
                "late_by_ms": 0,
                "is_estimated": False,
                "counter_reset": False,
            },
        },
        {
            **common,
            "observed_at": _timestamp(finished_at),
            "event_type": final_type,
            "severity": severity,
            "message": _completion_message(chat, upload),
        },
    ]


def transfer_kind_for_file(file_name: str) -> str:
    """Classify a demo payload only for UI grouping, never by inspecting its content."""

    suffix = Path(file_name).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
        return "photo"
    if suffix in {".mp4", ".mov", ".mkv", ".webm"}:
        return "video"
    return "file"


def _accepted_count(response: httpx.Response) -> int:
    try:
        payload = response.json()
    except ValueError as error:
        raise CollectorPublishError("collector response was not JSON") from error
    accepted = payload.get("accepted") if isinstance(payload, dict) else None
    if isinstance(accepted, bool) or not isinstance(accepted, int) or accepted < 0:
        raise CollectorPublishError("collector response did not contain a valid accepted count")
    return accepted


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _require_utc(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TrafficDemoError("telemetry timestamps must include a UTC offset")


def _completion_message(chat: ProbeMeasurements, upload: UploadMeasurement) -> str:
    state = "verified" if upload.integrity == "verified" else "not verified"
    return (
        "Local traffic demo finished: "
        f"{chat.received}/{chat.attempted} chat echoes, "
        f"{upload.bytes_transferred}/{upload.bytes_expected} upload bytes, checksum {state}."
    )


def _content_type(file_name: str) -> str:
    suffix = Path(file_name).suffix.lower()
    mapping = {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".mkv": "video/x-matroska",
        ".mov": "video/quicktime",
        ".mp4": "video/mp4",
        ".png": "image/png",
        ".webm": "video/webm",
        ".webp": "image/webp",
    }
    return mapping.get(suffix, "application/octet-stream")
