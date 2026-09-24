"""Private-LAN browser workload for a two-laptop PRAHARI demonstration."""

from __future__ import annotations

import asyncio
import hmac
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from prahari.traffic_demo.protocol import (
    ProbeMeasurements,
    ReceiverLimits,
    TrafficDemoError,
    UploadMeasurement,
    validate_filename,
    validate_shared_token,
)
from prahari.traffic_demo.receiver import validate_bind_options
from prahari.traffic_demo.telemetry import (
    CollectorPublishError,
    TrafficTelemetryContext,
    TrafficTelemetryPublisher,
    build_app_probe_records,
    build_event_records,
    transfer_kind_for_file,
)

MAX_ACTIVE_UPLOADS = 4
MAX_UPLOAD_SECONDS = 30.0
ACK_TTL_SECONDS = 30.0
MAX_RTT_MS = 60_000.0
RECENT_RTT_SAMPLES = 20


class ChatRequest(BaseModel):
    """One bounded message whose content is echoed and immediately forgotten."""

    message: str = Field(min_length=1, max_length=4_096)


class ChatAcknowledgement(BaseModel):
    """Browser-observed round-trip time for a previously echoed message."""

    message_id: str = Field(pattern=r"^message-[0-9a-f]{32}$")
    rtt_ms: float = Field(ge=0, le=MAX_RTT_MS, allow_inf_nan=False)


@dataclass(frozen=True)
class UserDemoConfig:
    """Configuration for the browser workload and its local evidence sink."""

    assets_directory: Path
    access_token: str | None = None
    publisher: TrafficTelemetryPublisher | None = None
    limits: ReceiverLimits = field(default_factory=ReceiverLimits)

    def __post_init__(self) -> None:
        validate_shared_token(self.access_token, required=False)
        if not self.assets_directory.is_dir():
            raise TrafficDemoError("user-demo assets directory does not exist")


class _SessionMeasurements:
    """Small process-local measurement window; it never contains message text."""

    def __init__(self) -> None:
        self.pending: dict[str, float] = {}
        self.rtts_ms: deque[float] = deque(maxlen=RECENT_RTT_SAMPLES)

    def register_message(self) -> str:
        now = time.monotonic()
        self.pending = {
            message_id: issued_at
            for message_id, issued_at in self.pending.items()
            if now - issued_at <= ACK_TTL_SECONDS
        }
        message_id = f"message-{uuid4().hex}"
        self.pending[message_id] = now
        return message_id

    def acknowledge(self, message_id: str, rtt_ms: float) -> None:
        issued_at = self.pending.pop(message_id, None)
        if issued_at is None or time.monotonic() - issued_at > ACK_TTL_SECONDS:
            raise TrafficDemoError("chat acknowledgement is unknown or expired")
        if not math.isfinite(rtt_ms) or not 0 <= rtt_ms <= MAX_RTT_MS:
            raise TrafficDemoError("chat RTT is outside the permitted range")
        self.rtts_ms.append(rtt_ms)

    def chat_summary(self) -> ProbeMeasurements:
        samples = tuple(self.rtts_ms)
        if not samples:
            raise TrafficDemoError("send and acknowledge a chat message before uploading")
        return ProbeMeasurements(attempted=len(samples), received=len(samples), rtts_ms=samples)


def create_user_demo_app(config: UserDemoConfig) -> FastAPI:
    """Create a same-origin browser service that emits existing PRAHARI evidence."""

    app = FastAPI(
        title="PRAHARI two-laptop user demo",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    measurements = _SessionMeasurements()
    upload_slots = asyncio.Semaphore(MAX_ACTIVE_UPLOADS)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/")
    async def home() -> FileResponse:
        return FileResponse(config.assets_directory / "index.html", media_type="text/html")

    @app.get("/app.js")
    async def javascript() -> FileResponse:
        return FileResponse(
            config.assets_directory / "app.js",
            media_type="text/javascript",
        )

    @app.get("/styles.css")
    async def stylesheet() -> FileResponse:
        return FileResponse(config.assets_directory / "styles.css", media_type="text/css")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "mode": "private-lan-demo",
            "max_file_bytes": config.limits.max_file_bytes,
            "access_code_required": config.access_token is not None,
            "collector_configured": config.publisher is not None,
            "stores_messages_or_files": False,
        }

    @app.post("/api/chat")
    async def chat(
        payload: ChatRequest,
        access_code: Annotated[str | None, Header(alias="X-Prahari-Demo-Token")] = None,
    ) -> dict[str, object]:
        _require_access(config.access_token, access_code)
        message_id = measurements.register_message()
        return {
            "message_id": message_id,
            "echo": payload.message,
            "server_time": datetime.now(UTC).isoformat(),
            "stored": False,
        }

    @app.post("/api/chat/ack", status_code=status.HTTP_204_NO_CONTENT)
    async def acknowledge_chat(
        payload: ChatAcknowledgement,
        access_code: Annotated[str | None, Header(alias="X-Prahari-Demo-Token")] = None,
    ) -> Response:
        _require_access(config.access_token, access_code)
        try:
            measurements.acknowledge(payload.message_id, payload.rtt_ms)
        except TrafficDemoError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/api/upload")
    async def upload(
        request: Request,
        access_code: Annotated[str | None, Header(alias="X-Prahari-Demo-Token")] = None,
        file_name_header: Annotated[str | None, Header(alias="X-Prahari-File-Name")] = None,
    ) -> dict[str, object]:
        _require_access(config.access_token, access_code)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise HTTPException(status_code=415, detail="upload must use application/octet-stream")
        try:
            file_name = validate_filename(file_name_header or "")
            chat_summary = measurements.chat_summary()
        except TrafficDemoError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

        declared_length = _declared_length(request)
        if declared_length is not None and declared_length > config.limits.max_file_bytes:
            raise HTTPException(status_code=413, detail="upload exceeds the configured file limit")

        try:
            await asyncio.wait_for(upload_slots.acquire(), timeout=0.05)
        except TimeoutError as error:
            raise HTTPException(
                status_code=429,
                detail="the demo service is busy",
                headers={"Retry-After": "1"},
            ) from error

        started_at = datetime.now(UTC)
        started = time.monotonic()
        hasher = sha256()
        received = 0
        try:
            async with asyncio.timeout(MAX_UPLOAD_SECONDS):
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > config.limits.max_file_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail="upload exceeds the configured file limit",
                        )
                    hasher.update(chunk)
        except TimeoutError as error:
            raise HTTPException(status_code=408, detail="upload timed out") from error
        finally:
            upload_slots.release()

        if received < 1:
            raise HTTPException(status_code=422, detail="upload cannot be empty")
        if declared_length is not None and received != declared_length:
            raise HTTPException(
                status_code=400,
                detail="upload byte count did not match Content-Length",
            )

        finished_at = datetime.now(UTC)
        elapsed_seconds = max(time.monotonic() - started, 0.000_001)
        run_id = f"user-demo-{uuid4().hex}"
        transfer_id = f"transfer-{uuid4().hex}"
        checksum = hasher.hexdigest()
        upload_summary = UploadMeasurement(
            transfer_id=transfer_id,
            file_name=file_name,
            bytes_expected=received,
            bytes_transferred=received,
            bytes_received=received,
            elapsed_seconds=elapsed_seconds,
            checksum_sha256=checksum,
            integrity="verified",
            status="completed",
        )
        evidence_saved = False
        evidence_error: str | None = None
        if config.publisher is not None:
            context = TrafficTelemetryContext(run_id=run_id, source="two-laptop-user-demo")
            app_probes = build_app_probe_records(
                context=context,
                observed_at=finished_at,
                chat=chat_summary,
                upload=upload_summary,
                kind=transfer_kind_for_file(file_name),
            )
            events = build_event_records(
                context=context,
                started_at=started_at,
                finished_at=finished_at,
                chat=chat_summary,
                upload=upload_summary,
            )
            try:
                result = await config.publisher.publish(
                    app_probes=app_probes,
                    events=events,
                    run_id=run_id,
                )
                evidence_saved = result.app_probes_accepted == 2 and result.events_accepted == 2
                if not evidence_saved:
                    evidence_error = "collector did not accept the complete evidence bundle"
            except CollectorPublishError:
                evidence_error = "collector was unavailable or rejected the evidence"

        return {
            "run_id": run_id,
            "transfer_id": transfer_id,
            "file_name": file_name,
            "bytes_received": received,
            "checksum_sha256": checksum,
            "elapsed_ms": round(elapsed_seconds * 1_000, 3),
            "throughput_mbps": round(upload_summary.throughput_mbps, 3),
            "chat_samples": chat_summary.received,
            "chat_p95_ms": round(chat_summary.p95_rtt_ms, 3),
            "evidence_saved": evidence_saved,
            "evidence_error": evidence_error,
            "stored": False,
        }

    return app


def run_user_demo(
    *,
    host: str,
    port: int,
    config: UserDemoConfig,
    allow_non_loopback: bool = False,
) -> None:
    """Run the browser workload on loopback or an explicitly approved private address."""

    validate_bind_options(
        host,
        allow_non_loopback=allow_non_loopback,
        shared_token=config.access_token,
    )
    uvicorn.run(
        create_user_demo_app(config),
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        limit_concurrency=16,
        timeout_keep_alive=10,
    )


def _require_access(expected: str | None, supplied: str | None) -> None:
    if expected is None:
        return
    if supplied is None or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="access code was rejected")


def _declared_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        parsed = int(raw)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Content-Length is invalid") from error
    if parsed < 0:
        raise HTTPException(status_code=400, detail="Content-Length is invalid")
    return parsed
