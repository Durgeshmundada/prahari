"""Protocol validation and measurement helpers for the bounded traffic demo."""

from __future__ import annotations

import ipaddress
import json
import math
import re
from dataclasses import dataclass
from hashlib import sha256
from statistics import fmean
from typing import Any
from urllib.parse import urlsplit

TRAFFIC_DEMO_PROTOCOL = "prahari-traffic-demo/v1"
DEFAULT_MAX_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_TEXT_CHARS = 4_096
DEFAULT_MAX_CHUNK_BYTES = 64 * 1024
HARD_MAX_FILE_BYTES = 20 * 1024 * 1024
HARD_MAX_TEXT_CHARS = 4_096
HARD_MAX_CHUNK_BYTES = 1 * 1024 * 1024
MAX_CONTROL_MESSAGE_BYTES = 16 * 1024
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


class TrafficDemoError(ValueError):
    """A client-visible validation or protocol error in the local demo."""


@dataclass(frozen=True)
class ReceiverLimits:
    """Hard limits which keep one demo session bounded and memory-safe."""

    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS
    max_chunk_bytes: int = DEFAULT_MAX_CHUNK_BYTES

    def __post_init__(self) -> None:
        if self.max_file_bytes < 1:
            raise TrafficDemoError("max_file_bytes must be at least one byte")
        if self.max_file_bytes > HARD_MAX_FILE_BYTES:
            raise TrafficDemoError(
                "max_file_bytes cannot exceed the "
                f"{HARD_MAX_FILE_BYTES // (1024 * 1024)} MiB demo cap"
            )
        if self.max_text_chars < 1:
            raise TrafficDemoError("max_text_chars must be at least one character")
        if self.max_text_chars > HARD_MAX_TEXT_CHARS:
            raise TrafficDemoError("max_text_chars exceeds the hard demo cap")
        if self.max_chunk_bytes < 1:
            raise TrafficDemoError("max_chunk_bytes must be at least one byte")
        if self.max_chunk_bytes > HARD_MAX_CHUNK_BYTES:
            raise TrafficDemoError("max_chunk_bytes exceeds the hard demo cap")
        if self.max_chunk_bytes > self.max_file_bytes:
            raise TrafficDemoError("max_chunk_bytes cannot exceed max_file_bytes")


@dataclass(frozen=True)
class ProbeMeasurements:
    """Message-level delivery measurements, not raw IP packet measurements."""

    attempted: int
    received: int
    rtts_ms: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.attempted < 1:
            raise TrafficDemoError("at least one chat message must be attempted")
        if not 0 <= self.received <= self.attempted:
            raise TrafficDemoError("received chat count must be between zero and attempted")
        if len(self.rtts_ms) != self.received:
            raise TrafficDemoError("each received chat message must have one RTT sample")
        if any(value < 0 for value in self.rtts_ms):
            raise TrafficDemoError("RTT samples cannot be negative")

    @property
    def packet_loss_ratio(self) -> float:
        """Application-message loss ratio; WebSocket/TCP packet loss is not inferred."""

        return (self.attempted - self.received) / self.attempted

    @property
    def min_rtt_ms(self) -> float:
        return min(self.rtts_ms, default=0.0)

    @property
    def p50_rtt_ms(self) -> float:
        return percentile(self.rtts_ms, 50)

    @property
    def p95_rtt_ms(self) -> float:
        return percentile(self.rtts_ms, 95)

    @property
    def max_rtt_ms(self) -> float:
        return max(self.rtts_ms, default=0.0)

    @property
    def jitter_ms(self) -> float:
        """Mean absolute difference between consecutive application RTT samples."""

        if len(self.rtts_ms) < 2:
            return 0.0
        return fmean(
            abs(current - previous)
            for previous, current in zip(self.rtts_ms, self.rtts_ms[1:])
        )


@dataclass(frozen=True)
class UploadMeasurement:
    """End-to-end result for one bounded binary upload."""

    transfer_id: str
    file_name: str
    bytes_expected: int
    bytes_transferred: int
    bytes_received: int | None
    elapsed_seconds: float
    checksum_sha256: str
    integrity: str
    status: str
    error: str | None = None

    def __post_init__(self) -> None:
        validate_identifier(self.transfer_id, field_name="transfer_id")
        validate_filename(self.file_name)
        if self.bytes_expected < 0 or self.bytes_transferred < 0:
            raise TrafficDemoError("upload byte counts cannot be negative")
        if self.bytes_transferred > self.bytes_expected:
            raise TrafficDemoError("uploaded bytes cannot exceed expected bytes")
        if self.bytes_received is not None and not 0 <= self.bytes_received <= self.bytes_expected:
            raise TrafficDemoError("receiver byte count is outside the expected upload bounds")
        if self.elapsed_seconds < 0:
            raise TrafficDemoError("upload elapsed time cannot be negative")
        validate_sha256(self.checksum_sha256)
        if self.integrity not in {"pending", "verified", "failed"}:
            raise TrafficDemoError("upload integrity must be pending, verified, or failed")
        if self.status not in {"started", "completed", "failed", "cancelled"}:
            raise TrafficDemoError("upload status is invalid")
        if self.status == "completed" and (
            self.bytes_transferred != self.bytes_expected
            or self.bytes_received != self.bytes_expected
            or self.integrity != "verified"
        ):
            raise TrafficDemoError("a completed upload requires complete verified receiver receipt")

    @property
    def throughput_mbps(self) -> float:
        if self.elapsed_seconds <= 0:
            return 0.0
        return (self.bytes_transferred * 8) / (self.elapsed_seconds * 1_000_000)


def percentile(values: tuple[float, ...], percentile_value: int) -> float:
    """Return a nearest-rank percentile, deterministic for small demo samples."""

    if not values:
        return 0.0
    if not 0 <= percentile_value <= 100:
        raise TrafficDemoError("percentile must be between zero and 100")
    ordered = sorted(values)
    index = max(0, math.ceil((percentile_value / 100) * len(ordered)) - 1)
    return ordered[index]


def parse_control_message(raw: str) -> dict[str, Any]:
    """Parse and bound a JSON text frame before using any client-controlled fields."""

    if len(raw.encode("utf-8")) > MAX_CONTROL_MESSAGE_BYTES:
        raise TrafficDemoError("control message exceeds the demo size limit")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise TrafficDemoError("control frame must contain valid JSON") from error
    if not isinstance(payload, dict):
        raise TrafficDemoError("control frame must be a JSON object")
    return payload


def encode_control_message(payload: dict[str, Any]) -> str:
    """Encode a server/client control frame and assert that it remains bounded."""

    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > MAX_CONTROL_MESSAGE_BYTES:
        raise TrafficDemoError("generated control message exceeds the demo size limit")
    return encoded


def require_string(
    payload: dict[str, Any],
    field_name: str,
    *,
    max_length: int,
) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise TrafficDemoError(f"{field_name} must be a non-empty string")
    if len(value) > max_length:
        raise TrafficDemoError(f"{field_name} exceeds its size limit")
    return value


def require_int(
    payload: dict[str, Any],
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TrafficDemoError(f"{field_name} must be an integer")
    if not minimum <= value <= maximum:
        raise TrafficDemoError(f"{field_name} is outside the permitted range")
    return value


def validate_identifier(value: str, *, field_name: str = "identifier") -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise TrafficDemoError(
            f"{field_name} must be 1-100 characters of letters, digits, ., _, :, or -"
        )
    return value


def validate_filename(value: str) -> str:
    if not _SAFE_FILENAME.fullmatch(value) or value in {".", ".."}:
        raise TrafficDemoError(
            "file_name must be a plain 1-120 character filename without path separators"
        )
    return value


def validate_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise TrafficDemoError("checksum_sha256 must be a lowercase SHA-256 hex digest")
    return value


def sha256_hex(data: bytes) -> str:
    return sha256(data).hexdigest()


def make_demo_bytes(size: int) -> bytes:
    """Generate deterministic non-sensitive bytes without creating a file on disk."""

    if size < 0:
        raise TrafficDemoError("demo file size cannot be negative")
    return bytes((index * 31 + 17) % 256 for index in range(size))


def is_loopback_host(host: str) -> bool:
    """Recognise literal loopback addresses and localhost without DNS lookups."""

    normalized = host.strip().strip("[]").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def is_private_lan_host(host: str) -> bool:
    """Allow only literal RFC1918/ULA addresses for an explicitly exposed demo."""

    normalized = host.strip().strip("[]")
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        return False
    if address.version == 4:
        private_ranges = (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        )
        return any(address in network for network in private_ranges)
    return address in ipaddress.ip_network("fc00::/7")


def validate_receiver_url(value: str) -> tuple[str, bool]:
    """Validate the small fixed receiver surface and report whether it is loopback."""

    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"}:
        raise TrafficDemoError("receiver URL must use ws:// or wss://")
    if not parsed.hostname:
        raise TrafficDemoError("receiver URL must include a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TrafficDemoError("receiver URL cannot include credentials, query, or fragment")
    if parsed.path != "/ws/traffic":
        raise TrafficDemoError("receiver URL path must be /ws/traffic")
    return value, is_loopback_host(parsed.hostname)


def validate_shared_token(value: str | None, *, required: bool) -> str | None:
    """Reject weak/accidental shared tokens before an exposed lab listener starts."""

    if value is None or not value.strip():
        if required:
            raise TrafficDemoError(
                "a shared token of at least 16 characters is required off loopback"
            )
        return None
    if len(value) < 16 or len(value) > 256 or any(character.isspace() for character in value):
        raise TrafficDemoError(
            "shared token must be 16-256 non-whitespace characters; prefer an environment variable"
        )
    return value
