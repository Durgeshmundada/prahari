"""DC-side bounded WebSocket receiver for the local PRAHARI traffic demo."""

from __future__ import annotations

import asyncio
import hmac
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any
from uuid import uuid4

import uvicorn
from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from prahari.traffic_demo.protocol import (
    MAX_CONTROL_MESSAGE_BYTES,
    TRAFFIC_DEMO_PROTOCOL,
    ReceiverLimits,
    TrafficDemoError,
    encode_control_message,
    is_loopback_host,
    is_private_lan_host,
    parse_control_message,
    require_int,
    require_string,
    validate_filename,
    validate_identifier,
    validate_sha256,
    validate_shared_token,
)
from prahari.traffic_demo.storage import (
    ReceiverArtifactError,
    ReceiverArtifactExpiredError,
    ReceiverArtifactNotFoundError,
    ReceiverArtifactStore,
    validate_artifact_handle,
    validate_artifact_service_token,
)

MAX_ACTIVE_CONNECTIONS = 8
HELLO_TIMEOUT_SECONDS = 5.0
IDLE_TIMEOUT_SECONDS = 15.0
UPLOAD_PROGRESS_TIMEOUT_SECONDS = 10.0
MAX_UPLOAD_SECONDS = 30.0


@dataclass(frozen=True)
class ReceiverConfig:
    """Configuration for a bounded receiver and optional process-local artifact cache."""

    limits: ReceiverLimits = field(default_factory=ReceiverLimits)
    shared_token: str | None = None
    artifact_store: ReceiverArtifactStore | None = None
    artifact_service_token: str | None = None
    artifact_download_concurrency: int = 2

    def __post_init__(self) -> None:
        validate_shared_token(self.shared_token, required=False)
        validate_artifact_service_token(
            self.artifact_service_token,
            required=self.artifact_store is not None,
        )
        if self.artifact_store is None and self.artifact_service_token is not None:
            raise TrafficDemoError("artifact-service token requires an enabled artifact store")
        if (
            self.shared_token is not None
            and self.artifact_service_token is not None
            and hmac.compare_digest(self.shared_token, self.artifact_service_token)
        ):
            raise TrafficDemoError("WebSocket and artifact-service tokens must be different")
        if self.artifact_store is not None and (
            self.artifact_store.config.max_file_bytes < self.limits.max_file_bytes
        ):
            raise TrafficDemoError("artifact per-file limit must cover the receiver upload limit")
        if not 1 <= self.artifact_download_concurrency <= 16:
            raise TrafficDemoError("artifact download concurrency must be between 1 and 16")


@dataclass
class _ActiveUpload:
    run_id: str
    transfer_id: str
    file_name: str
    bytes_expected: int
    checksum_sha256: str
    retained_data: bytearray | None = None
    hasher: Any = field(default_factory=sha256)
    bytes_received: int = 0
    started_at_monotonic: float = field(default_factory=time.monotonic)
    last_progress_at_monotonic: float = field(default_factory=time.monotonic)

    def add_chunk(self, data: bytes, max_chunk_bytes: int) -> None:
        if not data:
            raise TrafficDemoError("empty binary chunks are not accepted")
        if len(data) > max_chunk_bytes:
            raise TrafficDemoError("binary chunk exceeds the demo chunk size limit")
        if self.bytes_received + len(data) > self.bytes_expected:
            raise TrafficDemoError("binary upload exceeds its declared size")
        self.hasher.update(data)
        if self.retained_data is not None:
            self.retained_data.extend(data)
        self.bytes_received += len(data)
        self.last_progress_at_monotonic = time.monotonic()

    def discard(self) -> None:
        if self.retained_data is not None:
            self.retained_data.clear()
        self.retained_data = None


@dataclass
class _ReceiverSession:
    authenticated: bool = False
    session_id: str | None = None
    upload: _ActiveUpload | None = None
    connected_at_monotonic: float = field(default_factory=time.monotonic)
    close_requested: bool = False


def create_receiver_app(config: ReceiverConfig | None = None) -> FastAPI:
    """Build the receiver app with a single bounded WebSocket endpoint.

    Upload bytes are hashed in memory. When explicitly configured, only an
    immutable checksum-verified receiver copy is retained until its short TTL.
    """

    receiver_config = config or ReceiverConfig()
    app = FastAPI(
        title="PRAHARI traffic-demo receiver",
        version="1.0.0",
        description="Local/demo WebSocket echo and bounded upload receiver; no device control.",
    )
    app.state.receiver_config = receiver_config
    connection_slots = asyncio.Semaphore(MAX_ACTIVE_CONNECTIONS)
    artifact_download_slots = asyncio.Semaphore(receiver_config.artifact_download_concurrency)

    @app.get("/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "mode": "local-demo-only",
            "protocol": TRAFFIC_DEMO_PROTOCOL,
            "max_file_bytes": receiver_config.limits.max_file_bytes,
            "stores_uploaded_files": receiver_config.artifact_store is not None,
            "shared_token_required": receiver_config.shared_token is not None,
            "artifact_service_token_required": receiver_config.artifact_store is not None,
        }

    @app.get("/api/traffic/artifact")
    async def download_artifact(request: Request) -> Response:
        store = _require_artifact_request(request, receiver_config)
        if request.headers.get("Range") is not None:
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail="artifact range requests are not supported",
            )
        handle = request.headers.get("X-Prahari-Artifact-Handle", "")
        try:
            validate_artifact_handle(handle)
        except ReceiverArtifactNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        acquired_slot = False
        try:
            try:
                await asyncio.wait_for(artifact_download_slots.acquire(), timeout=0.01)
                acquired_slot = True
            except TimeoutError as error:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="receiver artifact downloads are busy",
                    headers={"Retry-After": "1"},
                ) from error
            try:
                artifact = store.get_verified(handle)
            except ReceiverArtifactExpiredError as error:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(error)) from error
            except ReceiverArtifactNotFoundError as error:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=str(error),
                ) from error
            except ReceiverArtifactError as error:
                raise HTTPException(
                    status_code=status.HTTP_410_GONE,
                    detail="receiver artifact integrity is unavailable",
                ) from error
            return Response(
                content=artifact.data,
                media_type="application/octet-stream",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": _content_disposition(artifact.file_name),
                    "X-Content-Type-Options": "nosniff",
                    "X-Prahari-Artifact-SHA256": artifact.checksum_sha256,
                    "X-Prahari-Artifact-Run-Id": artifact.run_id,
                    "X-Prahari-Artifact-Transfer-Id": artifact.transfer_id,
                    "X-Prahari-Artifact-Expires-At": artifact.expires_at.isoformat(),
                },
            )
        finally:
            if acquired_slot:
                artifact_download_slots.release()

    @app.delete("/api/traffic/artifact", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_artifact(request: Request) -> Response:
        store = _require_artifact_request(request, receiver_config)
        handle = request.headers.get("X-Prahari-Artifact-Handle", "")
        try:
            deleted = store.delete(handle)
        except ReceiverArtifactNotFoundError as error:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="receiver artifact is unknown",
            )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.websocket("/ws/traffic")
    async def traffic_socket(websocket: WebSocket) -> None:
        # This receiver is a server-to-server probe target, never a browser
        # upload surface. Browsers send Origin on WebSocket handshakes.
        if websocket.headers.get("origin") is not None:
            await websocket.close(code=1008, reason="browser origins are not accepted")
            return
        await websocket.accept()
        acquired_slot = False
        try:
            await asyncio.wait_for(connection_slots.acquire(), timeout=0.01)
            acquired_slot = True
        except TimeoutError:
            await _send_error(
                websocket,
                "receiver_busy",
                "receiver has reached its connection limit",
            )
            await websocket.close(code=1013)
            return
        session = _ReceiverSession()
        try:
            while True:
                timeout_seconds = _next_receive_timeout(session)
                try:
                    incoming = await asyncio.wait_for(websocket.receive(), timeout=timeout_seconds)
                except TimeoutError:
                    await _close_for_timeout(websocket, session)
                    return
                if incoming["type"] == "websocket.disconnect":
                    return
                text = incoming.get("text")
                binary = incoming.get("bytes")
                if text is not None:
                    accepted = await _handle_control(websocket, session, receiver_config, text)
                elif binary is not None:
                    accepted = await _handle_binary(websocket, session, receiver_config, binary)
                else:
                    await _send_error(websocket, "unsupported_frame", "unsupported WebSocket frame")
                    accepted = False
                if session.close_requested:
                    return
                if not accepted:
                    await websocket.close(code=1008)
                    return
        except WebSocketDisconnect:
            return
        finally:
            if session.upload is not None:
                session.upload.discard()
                session.upload = None
            if acquired_slot:
                connection_slots.release()

    return app


async def _handle_control(
    websocket: WebSocket,
    session: _ReceiverSession,
    config: ReceiverConfig,
    raw: str,
) -> bool:
    try:
        payload = parse_control_message(raw)
        message_type = require_string(payload, "type", max_length=40)
        if message_type == "hello":
            await _handle_hello(websocket, session, config, payload)
            return session.authenticated
        if not session.authenticated:
            await _send_error(websocket, "handshake_required", "send hello before demo traffic")
            return False
        if message_type == "chat":
            await _handle_chat(websocket, config, payload)
        elif message_type == "file_start":
            await _handle_file_start(websocket, session, config, payload)
        elif message_type == "file_end":
            await _handle_file_end(websocket, session, config, payload)
        elif message_type == "close":
            await websocket.close(code=1000)
            session.close_requested = True
        else:
            await _send_error(websocket, "unsupported_message", "unsupported demo message type")
            return False
        return True
    except TrafficDemoError as error:
        await _send_error(websocket, "invalid_request", str(error))
        return False


async def _handle_hello(
    websocket: WebSocket,
    session: _ReceiverSession,
    config: ReceiverConfig,
    payload: dict[str, Any],
) -> None:
    if session.authenticated:
        raise TrafficDemoError("hello may only be sent once per connection")
    protocol = require_string(payload, "protocol", max_length=80)
    if protocol != TRAFFIC_DEMO_PROTOCOL:
        raise TrafficDemoError("unsupported traffic demo protocol version")
    if config.shared_token is not None:
        received_token = require_string(payload, "token", max_length=256)
        if not hmac.compare_digest(received_token, config.shared_token):
            raise TrafficDemoError("shared token was rejected")
    session.authenticated = True
    session.session_id = f"session-{uuid4().hex}"
    await _send_control(
        websocket,
        {
            "type": "hello_ack",
            "protocol": TRAFFIC_DEMO_PROTOCOL,
            "session_id": session.session_id,
            "max_file_bytes": config.limits.max_file_bytes,
            "max_chunk_bytes": config.limits.max_chunk_bytes,
            "stores_uploaded_files": config.artifact_store is not None,
        },
    )


async def _handle_chat(
    websocket: WebSocket,
    config: ReceiverConfig,
    payload: dict[str, Any],
) -> None:
    message_id = validate_identifier(require_string(payload, "message_id", max_length=100))
    message = require_string(payload, "message", max_length=config.limits.max_text_chars)
    await _send_control(
        websocket,
        {
            "type": "chat_echo",
            "message_id": message_id,
            "message": message,
        },
    )


async def _handle_file_start(
    websocket: WebSocket,
    session: _ReceiverSession,
    config: ReceiverConfig,
    payload: dict[str, Any],
) -> None:
    if session.upload is not None:
        raise TrafficDemoError("only one upload may be active on a demo connection")
    transfer_id = validate_identifier(require_string(payload, "transfer_id", max_length=100))
    file_name = validate_filename(require_string(payload, "file_name", max_length=120))
    bytes_expected = require_int(
        payload,
        "bytes_expected",
        minimum=1,
        maximum=config.limits.max_file_bytes,
    )
    checksum = validate_sha256(require_string(payload, "checksum_sha256", max_length=64))
    run_id_value = payload.get("run_id")
    run_id = (
        validate_identifier(require_string(payload, "run_id", max_length=100), field_name="run_id")
        if run_id_value is not None
        else f"receiver-{session.session_id or 'legacy'}"
    )
    session.upload = _ActiveUpload(
        run_id=run_id,
        transfer_id=transfer_id,
        file_name=file_name,
        bytes_expected=bytes_expected,
        checksum_sha256=checksum,
        retained_data=bytearray() if config.artifact_store is not None else None,
    )
    await _send_control(
        websocket,
        {
            "type": "file_ready",
            "transfer_id": transfer_id,
            "max_chunk_bytes": config.limits.max_chunk_bytes,
        },
    )


async def _handle_binary(
    websocket: WebSocket,
    session: _ReceiverSession,
    config: ReceiverConfig,
    data: bytes,
) -> bool:
    if not session.authenticated:
        await _send_error(websocket, "handshake_required", "send hello before binary data")
        return False
    if session.upload is None:
        await _send_error(websocket, "upload_not_started", "send file_start before binary data")
        return False
    try:
        session.upload.add_chunk(data, config.limits.max_chunk_bytes)
    except TrafficDemoError as error:
        session.upload.discard()
        session.upload = None
        await _send_error(websocket, "invalid_upload", str(error))
        return False
    return True


async def _handle_file_end(
    websocket: WebSocket,
    session: _ReceiverSession,
    config: ReceiverConfig,
    payload: dict[str, Any],
) -> None:
    upload = session.upload
    transfer_id = validate_identifier(require_string(payload, "transfer_id", max_length=100))
    if upload is None:
        raise TrafficDemoError("no upload is active")
    if transfer_id != upload.transfer_id:
        raise TrafficDemoError("file_end transfer_id does not match the active upload")
    session.upload = None
    checksum = upload.hasher.hexdigest()
    is_complete = upload.bytes_received == upload.bytes_expected
    checksum_matches = hmac.compare_digest(checksum, upload.checksum_sha256)
    if not is_complete or not checksum_matches:
        upload.discard()
        await _send_control(
            websocket,
            {
                "type": "file_received",
                "transfer_id": upload.transfer_id,
                "bytes_received": upload.bytes_received,
                "checksum_sha256": checksum,
                "integrity": "failed",
                "status": "failed",
                "error": "size or checksum verification failed",
            },
        )
        return
    stored_artifact = None
    if config.artifact_store is not None and upload.retained_data is not None:
        try:
            stored_artifact = config.artifact_store.put_verified(
                run_id=upload.run_id,
                transfer_id=upload.transfer_id,
                file_name=upload.file_name,
                data=bytes(upload.retained_data),
                checksum_sha256=checksum,
            )
        except ReceiverArtifactError:
            stored_artifact = None
    upload.discard()
    receipt: dict[str, Any] = {
        "type": "file_received",
        "transfer_id": upload.transfer_id,
        "bytes_received": upload.bytes_received,
        "checksum_sha256": checksum,
        "integrity": "verified",
        "status": "completed",
        "error": None,
    }
    if stored_artifact is not None:
        receipt["artifact"] = {
            "handle": stored_artifact.handle,
            "expires_at": stored_artifact.expires_at.isoformat(),
        }
    await _send_control(websocket, receipt)


async def _send_control(websocket: WebSocket, payload: dict[str, Any]) -> None:
    await websocket.send_text(encode_control_message(payload))


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    await _send_control(websocket, {"type": "error", "code": code, "message": message})


def _next_receive_timeout(session: _ReceiverSession) -> float:
    """Use absolute handshaking and upload-progress deadlines, not frame activity."""

    now = time.monotonic()
    if not session.authenticated:
        deadline = session.connected_at_monotonic + HELLO_TIMEOUT_SECONDS
    elif session.upload is not None:
        upload = session.upload
        deadline = min(
            upload.started_at_monotonic + MAX_UPLOAD_SECONDS,
            upload.last_progress_at_monotonic + UPLOAD_PROGRESS_TIMEOUT_SECONDS,
        )
    else:
        deadline = now + IDLE_TIMEOUT_SECONDS
    return max(0.0, deadline - now)


async def _close_for_timeout(websocket: WebSocket, session: _ReceiverSession) -> None:
    if not session.authenticated:
        code = "handshake_timeout"
        message = "hello was not received before the receiver timeout"
    elif session.upload is not None:
        elapsed = time.monotonic() - session.upload.started_at_monotonic
        if elapsed >= MAX_UPLOAD_SECONDS:
            code = "upload_duration_exceeded"
            message = "upload exceeded the receiver's maximum total duration"
        else:
            code = "upload_timeout"
            message = "upload progress stopped before the receiver timeout"
    else:
        code = "idle_timeout"
        message = "demo session was idle before the receiver timeout"
    await _send_error(websocket, code, message)
    await websocket.close(code=1008)


def _require_artifact_request(
    request: Request,
    config: ReceiverConfig,
) -> ReceiverArtifactStore:
    if request.headers.get("Origin") is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="browser origins are not accepted by the receiver artifact service",
        )
    if config.artifact_store is None or config.artifact_service_token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="receiver artifact service is disabled",
        )
    authorization = request.headers.get("Authorization", "")
    prefix = "Bearer "
    supplied_token = authorization.removeprefix(prefix) if authorization.startswith(prefix) else ""
    if not supplied_token or not hmac.compare_digest(
        supplied_token,
        config.artifact_service_token,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="receiver artifact credential was rejected",
        )
    return config.artifact_store


def _content_disposition(file_name: str) -> str:
    safe_name = validate_filename(file_name)
    return f'attachment; filename="{safe_name}"'


def validate_bind_options(
    host: str,
    *,
    allow_non_loopback: bool,
    shared_token: str | None,
) -> str | None:
    """Require explicit consent and a token before binding beyond loopback."""

    if is_loopback_host(host):
        return validate_shared_token(shared_token, required=False)
    if not allow_non_loopback:
        raise TrafficDemoError(
            "receiver only binds to loopback by default; pass --allow-non-loopback explicitly"
        )
    if not is_private_lan_host(host):
        raise TrafficDemoError(
            "an exposed receiver must bind to a literal RFC1918/ULA private-LAN address, "
            "not a public or unspecified address"
        )
    return validate_shared_token(shared_token, required=True)


def run_receiver(
    *,
    host: str,
    port: int,
    config: ReceiverConfig,
    allow_non_loopback: bool = False,
) -> None:
    """Run the local receiver through Uvicorn; it has no device-control path."""

    validate_bind_options(
        host,
        allow_non_loopback=allow_non_loopback,
        shared_token=config.shared_token,
    )
    uvicorn.run(
        create_receiver_app(config),
        host=host,
        port=port,
        log_level="info",
        limit_concurrency=16,
        ws_max_size=max(config.limits.max_chunk_bytes, MAX_CONTROL_MESSAGE_BYTES),
        ws_max_queue=8,
        ws_ping_interval=20.0,
        ws_ping_timeout=20.0,
        # Binary demo payloads may grow when per-message compression is
        # attempted. Keep frames within the negotiated chunk limit instead.
        ws_per_message_deflate=False,
    )
