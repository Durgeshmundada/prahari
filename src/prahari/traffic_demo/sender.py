"""Branch-side WebSocket traffic generator and PRAHARI evidence publisher."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from prahari.traffic_demo.protocol import (
    DEFAULT_MAX_CHUNK_BYTES,
    DEFAULT_MAX_FILE_BYTES,
    HARD_MAX_CHUNK_BYTES,
    HARD_MAX_FILE_BYTES,
    MAX_CONTROL_MESSAGE_BYTES,
    TRAFFIC_DEMO_PROTOCOL,
    ProbeMeasurements,
    TrafficDemoError,
    UploadMeasurement,
    encode_control_message,
    is_private_lan_host,
    make_demo_bytes,
    parse_control_message,
    require_int,
    require_string,
    sha256_hex,
    validate_filename,
    validate_identifier,
    validate_receiver_url,
    validate_sha256,
    validate_shared_token,
)
from prahari.traffic_demo.storage import (
    ReceiverArtifactNotFoundError,
    validate_artifact_handle,
)
from prahari.traffic_demo.telemetry import (
    CollectorPublishError,
    CollectorPublishResult,
    TelemetryPublisher,
    TrafficTelemetryContext,
    TrafficTelemetryPublisher,
    build_app_probe_records,
    build_event_records,
    transfer_kind_for_file,
)


class RemoteTrafficError(RuntimeError):
    """The bounded receiver rejected a protocol message or upload."""


@dataclass(frozen=True)
class TrafficProbeConfig:
    """Safe inputs for one branch-to-DC application traffic run."""

    receiver_url: str = "ws://127.0.0.1:8765/ws/traffic"
    collector_url: str | None = "http://127.0.0.1:8000"
    shared_token: str | None = None
    allow_non_loopback_receiver: bool = False
    allow_insecure_remote_ws: bool = False
    chat_messages: int = 5
    message_interval_seconds: float = 0.05
    timeout_seconds: float = 5.0
    generated_file_bytes: int = 128 * 1024
    sample_file: Path | None = None
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES
    run_id: str = field(default_factory=lambda: f"traffic-demo-{uuid4().hex[:16]}")
    path_id: str = "path:branch-01-to-dc:primary"
    topology_version: str = "lab-v1"
    config_version: str = "lab-config-v1"

    def __post_init__(self) -> None:
        _, receiver_is_loopback = validate_receiver_url(self.receiver_url)
        receiver_url = urlsplit(self.receiver_url)
        if not receiver_is_loopback and not self.allow_non_loopback_receiver:
            raise TrafficDemoError(
                "remote receiver needs --allow-non-loopback-receiver and a shared token"
            )
        if not receiver_is_loopback and not is_private_lan_host(receiver_url.hostname or ""):
            raise TrafficDemoError(
                "a remote receiver must use a literal RFC1918/ULA private-LAN address"
            )
        if (
            not receiver_is_loopback
            and receiver_url.scheme == "ws"
            and not self.allow_insecure_remote_ws
        ):
            raise TrafficDemoError(
                "remote traffic should use wss://; --allow-insecure-remote-ws is required "
                "only for an isolated private lab"
            )
        validate_shared_token(self.shared_token, required=not receiver_is_loopback)
        if not 1 <= self.chat_messages <= 100:
            raise TrafficDemoError("chat_messages must be between 1 and 100")
        if not 0 <= self.message_interval_seconds <= 5:
            raise TrafficDemoError("message interval must be between 0 and 5 seconds")
        if not 0.1 <= self.timeout_seconds <= 60:
            raise TrafficDemoError("timeout must be between 0.1 and 60 seconds")
        if not 1 <= self.max_file_bytes <= HARD_MAX_FILE_BYTES:
            raise TrafficDemoError(
                "max file size must be between 1 byte and "
                f"{HARD_MAX_FILE_BYTES // (1024 * 1024)} MiB"
            )
        if not 1 <= self.chunk_bytes <= min(DEFAULT_MAX_CHUNK_BYTES, self.max_file_bytes):
            raise TrafficDemoError("chunk size must be between 1 byte and 64 KiB")
        if self.sample_file is not None and self.generated_file_bytes != 128 * 1024:
            raise TrafficDemoError("choose either a sample file or a generated file size, not both")
        if self.sample_file is None and not 1 <= self.generated_file_bytes <= self.max_file_bytes:
            raise TrafficDemoError("generated file size must fit within max_file_bytes")
        validate_identifier(self.run_id, field_name="run_id")


@dataclass(frozen=True)
class PreparedPayload:
    """In-memory payload, bounded before a WebSocket connection is opened."""

    file_name: str
    data: bytes
    kind: str


@dataclass(frozen=True)
class ReceiverArtifactReceipt:
    """Private receiver handle retained only inside the API process."""

    handle: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class _FileSendResult:
    upload: UploadMeasurement
    receiver_artifact: ReceiverArtifactReceipt | None = field(default=None, repr=False)


@dataclass(frozen=True)
class TrafficDemoResult:
    """Outcome available to the terminal and normalized collector publisher."""

    run_id: str
    session_id: str | None
    started_at: datetime
    finished_at: datetime
    chat: ProbeMeasurements
    upload: UploadMeasurement
    collector_result: CollectorPublishResult | None = None
    collector_error: str | None = None
    run_error: str | None = None
    receiver_artifact: ReceiverArtifactReceipt | None = field(default=None, repr=False)

    @property
    def passed(self) -> bool:
        return (
            self.run_error is None
            and self.chat.received == self.chat.attempted
            and self.upload.status == "completed"
            and self.upload.integrity == "verified"
            and self.collector_error is None
            and (self.collector_result is None or self.evidence_saved)
        )

    @property
    def evidence_saved(self) -> bool:
        return (
            self.collector_error is None
            and self.collector_result is not None
            and self.collector_result.events_accepted == 2
            and self.collector_result.app_probes_accepted == 2
        )

    def summary(self) -> dict[str, object]:
        """A terminal-safe result with metadata and measurements, never file bytes or tokens."""

        return {
            "run_id": self.run_id,
            "status": "passed" if self.passed else "failed",
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "chat": {
                "attempted": self.chat.attempted,
                "received": self.chat.received,
                "application_message_loss_ratio": self.chat.packet_loss_ratio,
                "rtt_ms": {
                    "min": self.chat.min_rtt_ms,
                    "p50": self.chat.p50_rtt_ms,
                    "p95": self.chat.p95_rtt_ms,
                    "max": self.chat.max_rtt_ms,
                },
                "jitter_ms": self.chat.jitter_ms,
            },
            "upload": {
                "transfer_id": self.upload.transfer_id,
                "file_name": self.upload.file_name,
                "bytes_expected": self.upload.bytes_expected,
                "bytes_transferred": self.upload.bytes_transferred,
                "bytes_received": self.upload.bytes_received,
                "elapsed_seconds": self.upload.elapsed_seconds,
                "throughput_mbps": self.upload.throughput_mbps,
                "integrity": self.upload.integrity,
                "status": self.upload.status,
                "error": self.upload.error,
            },
            "collector": (
                {
                    "app_probes_accepted": self.collector_result.app_probes_accepted,
                    "events_accepted": self.collector_result.events_accepted,
                }
                if self.collector_result is not None
                else None
            ),
            "collector_error": self.collector_error,
            "evidence_saved": self.evidence_saved,
            "run_error": self.run_error,
        }


async def run_traffic_demo(
    config: TrafficProbeConfig,
    *,
    publisher: TrafficTelemetryPublisher | None = None,
    prepared_payload: PreparedPayload | None = None,
) -> TrafficDemoResult:
    """Run bounded chat/upload traffic and publish its normalized evidence.

    A network/protocol failure still produces failed app-probe/event records when
    a collector is configured, so the dashboard can distinguish failure from a
    missing test run. Programmer/configuration errors are raised instead.
    """

    prepared = prepared_payload or prepare_payload(config)
    _validate_prepared_payload(prepared, max_file_bytes=config.max_file_bytes)
    started_at = datetime.now(UTC)
    session_id: str | None = None
    chat = ProbeMeasurements(attempted=config.chat_messages, received=0, rtts_ms=())
    upload = _failed_upload(prepared, "not started")
    receiver_artifact: ReceiverArtifactReceipt | None = None
    run_error: str | None = None
    try:
        async with connect(
            config.receiver_url,
            open_timeout=config.timeout_seconds,
            close_timeout=config.timeout_seconds,
            max_size=MAX_CONTROL_MESSAGE_BYTES,
            compression=None,
        ) as websocket:
            session_id, receiver_max_file_bytes, receiver_max_chunk_bytes = await _hello(
                websocket,
                config,
            )
            if len(prepared.data) > receiver_max_file_bytes:
                raise TrafficDemoError("payload exceeds the receiver's configured file size limit")
            chat = await _send_chat_messages(websocket, config)
            file_result = await _send_file(
                websocket,
                prepared,
                config,
                receiver_max_chunk_bytes=receiver_max_chunk_bytes,
            )
            upload = file_result.upload
            receiver_artifact = file_result.receiver_artifact
            try:
                await websocket.send(encode_control_message({"type": "close"}))
            except (ConnectionClosed, OSError, WebSocketException):
                # The verified receipt and artifact handle are authoritative;
                # failure of the courtesy close frame must not erase them.
                pass
    except (
        TimeoutError,
        ConnectionClosed,
        OSError,
        RemoteTrafficError,
        TrafficDemoError,
        WebSocketException,
    ) as error:
        run_error = _safe_error(error)
        if not (
            upload.status == "completed"
            and upload.integrity == "verified"
            and upload.bytes_transferred == upload.bytes_expected
            and upload.bytes_received == upload.bytes_expected
        ):
            receiver_artifact = None
            upload = _failed_upload(
                prepared,
                run_error,
                upload.bytes_transferred,
                bytes_received=upload.bytes_received,
            )

    finished_at = datetime.now(UTC)
    context = TrafficTelemetryContext(
        run_id=config.run_id,
        path_id=config.path_id,
        topology_version=config.topology_version,
        config_version=config.config_version,
    )
    app_probes = build_app_probe_records(
        context=context,
        observed_at=finished_at,
        chat=chat,
        upload=upload,
        kind=prepared.kind,
    )
    events = build_event_records(
        context=context,
        started_at=started_at,
        finished_at=finished_at,
        chat=chat,
        upload=upload,
    )
    collector_result: CollectorPublishResult | None = None
    collector_error: str | None = None
    if publisher is None and config.collector_url is not None:
        publisher = TelemetryPublisher(config.collector_url, timeout_seconds=config.timeout_seconds)
    if publisher is not None:
        try:
            collector_result = await publisher.publish(
                app_probes=app_probes,
                events=events,
                run_id=config.run_id,
            )
        except CollectorPublishError as error:
            collector_error = _safe_error(error)
    return TrafficDemoResult(
        run_id=config.run_id,
        session_id=session_id,
        started_at=started_at,
        finished_at=finished_at,
        chat=chat,
        upload=upload,
        collector_result=collector_result,
        collector_error=collector_error,
        run_error=run_error,
        receiver_artifact=receiver_artifact,
    )


async def run_periodic_traffic_demo(
    config: TrafficProbeConfig,
    *,
    repetitions: int | None,
    cadence_seconds: float = 5.0,
    publisher: TrafficTelemetryPublisher | None = None,
    on_result: Callable[[TrafficDemoResult], object] | None = None,
    retain_results: bool | None = None,
) -> list[TrafficDemoResult]:
    """Repeat a probe at live-ready cadence; ``None`` runs until interrupted.

    PRAHARI considers app-probe evidence stale after roughly two sampling
    intervals. A 5-second cadence therefore keeps the demo evidence fresh while
    it is running. This function does not perform network/device changes.
    """

    if repetitions is not None and not 1 <= repetitions <= 10_000:
        raise TrafficDemoError("repetitions must be between 1 and 10,000, or None")
    if not 1 <= cadence_seconds <= 10:
        raise TrafficDemoError("cadence must be between 1 and 10 seconds for live readiness")
    if retain_results is None:
        retain_results = repetitions is not None
    results: list[TrafficDemoResult] = []
    completed = 0
    while repetitions is None or completed < repetitions:
        cycle_started = time.perf_counter()
        result = await run_traffic_demo(config, publisher=publisher)
        if retain_results:
            results.append(result)
        if on_result is not None:
            callback_result = on_result(result)
            if inspect.isawaitable(callback_result):
                await callback_result
        completed += 1
        if repetitions is not None and completed >= repetitions:
            break
        remaining = cadence_seconds - (time.perf_counter() - cycle_started)
        if remaining > 0:
            await asyncio.sleep(remaining)
    return results


def prepare_payload(config: TrafficProbeConfig) -> PreparedPayload:
    """Read at most the declared byte limit, or generate a safe deterministic payload."""

    if config.sample_file is None:
        data = make_demo_bytes(config.generated_file_bytes)
        return PreparedPayload(file_name="prahari-demo.bin", data=data, kind="file")
    path = config.sample_file.expanduser()
    if not path.is_file():
        raise TrafficDemoError("sample file must be an existing regular file")
    file_name = validate_filename(path.name)
    file_size = path.stat().st_size
    if not 1 <= file_size <= config.max_file_bytes:
        raise TrafficDemoError("sample file size is outside the configured bounded demo limit")
    with path.open("rb") as file_handle:
        data = file_handle.read(config.max_file_bytes + 1)
    if len(data) != file_size:
        raise TrafficDemoError("sample file changed while it was being read")
    return PreparedPayload(file_name=file_name, data=data, kind=transfer_kind_for_file(file_name))


def prepare_uploaded_payload(
    file_name: str,
    data: bytes,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> PreparedPayload:
    """Prepare already-uploaded bytes without writing operator content to disk."""

    prepared = PreparedPayload(
        file_name=validate_filename(file_name),
        data=data,
        kind=transfer_kind_for_file(file_name),
    )
    _validate_prepared_payload(prepared, max_file_bytes=max_file_bytes)
    return prepared


def _validate_prepared_payload(
    prepared: PreparedPayload,
    *,
    max_file_bytes: int,
) -> None:
    validate_filename(prepared.file_name)
    if prepared.kind not in {"photo", "video", "file"}:
        raise TrafficDemoError("prepared payload kind must be photo, video, or file")
    if not 1 <= len(prepared.data) <= max_file_bytes:
        raise TrafficDemoError("uploaded file size is outside the configured bounded demo limit")


async def _hello(
    websocket: ClientConnection,
    config: TrafficProbeConfig,
) -> tuple[str, int, int]:
    payload: dict[str, Any] = {"type": "hello", "protocol": TRAFFIC_DEMO_PROTOCOL}
    if config.shared_token is not None:
        payload["token"] = config.shared_token
    await websocket.send(encode_control_message(payload))
    response = await _receive_control(websocket, config.timeout_seconds)
    _raise_if_receiver_error(response)
    if response.get("type") != "hello_ack":
        raise RemoteTrafficError("receiver did not acknowledge the traffic-demo handshake")
    session_id = validate_identifier(require_string(response, "session_id", max_length=100))
    max_file_bytes = require_int(
        response,
        "max_file_bytes",
        minimum=1,
        maximum=HARD_MAX_FILE_BYTES,
    )
    max_chunk_bytes = require_int(
        response,
        "max_chunk_bytes",
        minimum=1,
        maximum=HARD_MAX_CHUNK_BYTES,
    )
    return session_id, max_file_bytes, max_chunk_bytes


async def _send_chat_messages(
    websocket: ClientConnection,
    config: TrafficProbeConfig,
) -> ProbeMeasurements:
    """Measure each actual chat echo without using a burst queueing delay as RTT."""

    sent_at: dict[str, float] = {}
    message_order: list[str] = []
    rtt_by_message_id: dict[str, float] = {}
    for sequence in range(config.chat_messages):
        message_id = f"chat-{sequence + 1}-{uuid4().hex[:10]}"
        message_order.append(message_id)
        sent_at[message_id] = time.perf_counter()
        await websocket.send(
            encode_control_message(
                {
                    "type": "chat",
                    "message_id": message_id,
                    "message": f"PRAHARI local demo chat message {sequence + 1}",
                }
            )
        )
        await _collect_chat_echoes_until(
            websocket,
            sent_at=sent_at,
            rtt_by_message_id=rtt_by_message_id,
            target_message_id=message_id,
            timeout_seconds=config.timeout_seconds,
        )
        if sequence + 1 < config.chat_messages and config.message_interval_seconds:
            await asyncio.sleep(config.message_interval_seconds)

    # One final bounded drain accepts any echo which arrived after an individual
    # request timeout, without turning the next message's RTT into a queue delay.
    if len(rtt_by_message_id) < config.chat_messages:
        await _collect_chat_echoes_until(
            websocket,
            sent_at=sent_at,
            rtt_by_message_id=rtt_by_message_id,
            target_message_id=None,
            timeout_seconds=config.timeout_seconds,
        )
    rtts = tuple(rtt_by_message_id[item] for item in message_order if item in rtt_by_message_id)
    return ProbeMeasurements(
        attempted=config.chat_messages,
        received=len(rtts),
        rtts_ms=rtts,
    )


async def _collect_chat_echoes_until(
    websocket: ClientConnection,
    *,
    sent_at: dict[str, float],
    rtt_by_message_id: dict[str, float],
    target_message_id: str | None,
    timeout_seconds: float,
) -> None:
    deadline = time.perf_counter() + timeout_seconds
    while True:
        if target_message_id is not None and target_message_id in rtt_by_message_id:
            return
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        try:
            response = await _receive_control(websocket, remaining)
        except TimeoutError:
            return
        _raise_if_receiver_error(response)
        if response.get("type") != "chat_echo":
            raise RemoteTrafficError("receiver sent an unexpected response while chat was measured")
        message_id = validate_identifier(require_string(response, "message_id", max_length=100))
        if message_id not in sent_at or message_id in rtt_by_message_id:
            raise RemoteTrafficError("receiver returned an unknown or duplicate chat message ID")
        rtt_by_message_id[message_id] = (time.perf_counter() - sent_at[message_id]) * 1_000


async def _send_file(
    websocket: ClientConnection,
    prepared: PreparedPayload,
    config: TrafficProbeConfig,
    *,
    receiver_max_chunk_bytes: int,
) -> _FileSendResult:
    transfer_id = f"transfer-{uuid4().hex[:20]}"
    checksum = sha256_hex(prepared.data)
    bytes_sent = 0
    started = time.perf_counter()
    try:
        await websocket.send(
            encode_control_message(
                {
                    "type": "file_start",
                    "run_id": config.run_id,
                    "transfer_id": transfer_id,
                    "file_name": prepared.file_name,
                    "bytes_expected": len(prepared.data),
                    "checksum_sha256": checksum,
                }
            )
        )
        ready = await _receive_control(websocket, config.timeout_seconds)
        _raise_if_receiver_error(ready)
        if ready.get("type") != "file_ready":
            raise RemoteTrafficError("receiver did not accept the bounded upload")
        ready_transfer_id = validate_identifier(
            require_string(ready, "transfer_id", max_length=100)
        )
        if ready_transfer_id != transfer_id:
            raise RemoteTrafficError("receiver accepted a different transfer ID")
        negotiated_chunk_bytes = min(config.chunk_bytes, receiver_max_chunk_bytes)
        if negotiated_chunk_bytes < 1:
            raise RemoteTrafficError("receiver returned an invalid upload chunk size")
        for offset in range(0, len(prepared.data), negotiated_chunk_bytes):
            chunk = prepared.data[offset : offset + negotiated_chunk_bytes]
            await websocket.send(chunk)
            bytes_sent += len(chunk)
        await websocket.send(
            encode_control_message({"type": "file_end", "transfer_id": transfer_id})
        )
        acknowledgement = await _receive_control(websocket, config.timeout_seconds)
        _raise_if_receiver_error(acknowledgement)
        return _upload_from_acknowledgement(
            acknowledgement,
            transfer_id=transfer_id,
            prepared=prepared,
            bytes_sent=bytes_sent,
            elapsed_seconds=time.perf_counter() - started,
        )
    except (
        TimeoutError,
        ConnectionClosed,
        OSError,
        RemoteTrafficError,
        TrafficDemoError,
        WebSocketException,
    ) as error:
        return _FileSendResult(
            upload=_failed_upload(
                prepared,
                _safe_error(error),
                bytes_transferred=bytes_sent,
                transfer_id=transfer_id,
                elapsed_seconds=time.perf_counter() - started,
            )
        )


def _upload_from_acknowledgement(
    acknowledgement: dict[str, Any],
    *,
    transfer_id: str,
    prepared: PreparedPayload,
    bytes_sent: int,
    elapsed_seconds: float,
) -> _FileSendResult:
    if acknowledgement.get("type") != "file_received":
        raise RemoteTrafficError("receiver did not return a file receipt")
    response_transfer_id = validate_identifier(
        require_string(acknowledgement, "transfer_id", max_length=100)
    )
    if response_transfer_id != transfer_id:
        raise RemoteTrafficError("receiver returned a receipt for a different transfer")
    bytes_received = require_int(
        acknowledgement,
        "bytes_received",
        minimum=0,
        maximum=len(prepared.data),
    )
    checksum = validate_sha256(require_string(acknowledgement, "checksum_sha256", max_length=64))
    integrity = require_string(acknowledgement, "integrity", max_length=20)
    status = require_string(acknowledgement, "status", max_length=20)
    error_value = acknowledgement.get("error")
    if error_value is not None and not isinstance(error_value, str):
        raise RemoteTrafficError("receiver returned an invalid upload error")
    expected_checksum = sha256_hex(prepared.data)
    completed_and_verified = status == "completed" and integrity == "verified"
    receipt_matches_sender = (
        bytes_sent == len(prepared.data)
        and bytes_received == len(prepared.data)
        and hmac.compare_digest(checksum, expected_checksum)
    )
    if not completed_and_verified or not receipt_matches_sender:
        acknowledgement_error = error_value or "receiver receipt was incomplete or did not verify"
        return _FileSendResult(
            upload=_failed_upload(
                prepared,
                acknowledgement_error,
                bytes_transferred=bytes_sent,
                bytes_received=bytes_received,
                transfer_id=transfer_id,
                elapsed_seconds=elapsed_seconds,
                checksum_sha256=checksum,
            )
        )
    return _FileSendResult(
        upload=UploadMeasurement(
            transfer_id=transfer_id,
            file_name=prepared.file_name,
            bytes_expected=len(prepared.data),
            bytes_transferred=bytes_sent,
            bytes_received=bytes_received,
            elapsed_seconds=elapsed_seconds,
            checksum_sha256=checksum,
            integrity=integrity,
            status=status,
            error=error_value,
        ),
        receiver_artifact=_artifact_from_acknowledgement(acknowledgement),
    )


def _artifact_from_acknowledgement(
    acknowledgement: dict[str, Any],
) -> ReceiverArtifactReceipt | None:
    artifact = acknowledgement.get("artifact")
    if artifact is None:
        return None
    if not isinstance(artifact, dict):
        raise RemoteTrafficError("receiver returned invalid artifact metadata")
    handle = require_string(artifact, "handle", max_length=100)
    try:
        validate_artifact_handle(handle)
    except ReceiverArtifactNotFoundError as error:
        raise RemoteTrafficError("receiver returned an invalid artifact handle") from error
    expires_at_raw = require_string(artifact, "expires_at", max_length=50)
    try:
        expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise RemoteTrafficError("receiver returned an invalid artifact expiry") from error
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise RemoteTrafficError("receiver artifact expiry must include a UTC offset")
    expires_at = expires_at.astimezone(UTC)
    if expires_at <= datetime.now(UTC):
        raise RemoteTrafficError("receiver artifact expiry must be in the future")
    return ReceiverArtifactReceipt(
        handle=handle,
        expires_at=expires_at,
    )


async def _receive_control(websocket: ClientConnection, timeout_seconds: float) -> dict[str, Any]:
    raw = await asyncio.wait_for(websocket.recv(), timeout=timeout_seconds)
    if not isinstance(raw, str):
        raise RemoteTrafficError("receiver sent binary data where a control message was expected")
    return parse_control_message(raw)


def _raise_if_receiver_error(payload: dict[str, Any]) -> None:
    if payload.get("type") != "error":
        return
    message = payload.get("message")
    if not isinstance(message, str):
        message = "receiver rejected the traffic demo request"
    raise RemoteTrafficError(_safe_error(message))


def _failed_upload(
    prepared: PreparedPayload,
    error: str,
    bytes_transferred: int = 0,
    *,
    bytes_received: int | None = None,
    transfer_id: str | None = None,
    elapsed_seconds: float = 0.0,
    checksum_sha256: str | None = None,
) -> UploadMeasurement:
    return UploadMeasurement(
        transfer_id=transfer_id or f"transfer-{uuid4().hex[:20]}",
        file_name=prepared.file_name,
        bytes_expected=len(prepared.data),
        bytes_transferred=bytes_transferred,
        bytes_received=bytes_received,
        elapsed_seconds=elapsed_seconds,
        checksum_sha256=checksum_sha256 or sha256_hex(prepared.data),
        integrity="failed",
        status="failed",
        error=_safe_error(error),
    )


def _safe_error(value: object) -> str:
    """Keep errors bounded and avoid serialising credentials or binary content."""

    message = str(value).replace("\r", " ").replace("\n", " ").strip()
    return message[:500] or "traffic demo failed without a receiver response"
