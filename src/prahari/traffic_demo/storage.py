"""Bounded in-memory retention for receiver-verified demo artifacts."""

from __future__ import annotations

import hmac
import re
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from prahari.traffic_demo.protocol import HARD_MAX_FILE_BYTES, TrafficDemoError, validate_filename

_ARTIFACT_HANDLE = re.compile(r"^artifact-[0-9a-f]{32}$")


class ReceiverArtifactError(RuntimeError):
    """Base error for the receiver's process-local artifact cache."""


class ReceiverArtifactNotFoundError(ReceiverArtifactError):
    """The opaque artifact handle is unknown or has already been cleaned up."""


class ReceiverArtifactExpiredError(ReceiverArtifactError):
    """The requested artifact has passed its configured TTL."""


@dataclass(frozen=True)
class ReceiverArtifactStoreConfig:
    max_file_bytes: int = 5 * 1024 * 1024
    max_total_bytes: int = 25 * 1024 * 1024
    max_items: int = 10
    ttl_seconds: int = 10 * 60

    def __post_init__(self) -> None:
        if not 1 <= self.max_file_bytes <= HARD_MAX_FILE_BYTES:
            raise ReceiverArtifactError("artifact per-file limit is outside the hard demo cap")
        if not self.max_file_bytes <= self.max_total_bytes <= 1024 * 1024 * 1024:
            raise ReceiverArtifactError(
                "artifact total limit must cover one file and cannot exceed 1 GiB"
            )
        if not 1 <= self.max_items <= 1_000:
            raise ReceiverArtifactError("artifact count limit must be between 1 and 1,000")
        if not 30 <= self.ttl_seconds <= 24 * 60 * 60:
            raise ReceiverArtifactError("artifact TTL must be between 30 seconds and 24 hours")


@dataclass(frozen=True)
class ReceiverArtifact:
    handle: str
    run_id: str
    transfer_id: str
    file_name: str
    size_bytes: int
    checksum_sha256: str
    created_at: datetime
    expires_at: datetime
    data: bytes = field(repr=False)


class ReceiverArtifactStore:
    """Keep immutable verified bytes within count, byte, and TTL limits."""

    def __init__(self, config: ReceiverArtifactStoreConfig | None = None) -> None:
        self.config = config or ReceiverArtifactStoreConfig()
        self._artifacts: dict[str, ReceiverArtifact] = {}
        self._total_bytes = 0
        self._lock = threading.RLock()

    def put_verified(
        self,
        *,
        run_id: str,
        transfer_id: str,
        file_name: str,
        data: bytes,
        checksum_sha256: str,
    ) -> ReceiverArtifact:
        validate_filename(file_name)
        immutable_data = bytes(data)
        size_bytes = len(immutable_data)
        if not 1 <= size_bytes <= self.config.max_file_bytes:
            raise ReceiverArtifactError("verified artifact is outside the per-file limit")
        actual_checksum = sha256(immutable_data).hexdigest()
        if not hmac.compare_digest(actual_checksum, checksum_sha256):
            raise ReceiverArtifactError("verified artifact checksum does not match its bytes")
        now = datetime.now(UTC)
        artifact = ReceiverArtifact(
            handle=f"artifact-{uuid4().hex}",
            run_id=run_id,
            transfer_id=transfer_id,
            file_name=file_name,
            size_bytes=size_bytes,
            checksum_sha256=actual_checksum,
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.ttl_seconds),
            data=immutable_data,
        )
        with self._lock:
            self._cleanup_locked(now)
            self._require_capacity_locked(size_bytes)
            self._artifacts[artifact.handle] = artifact
            self._total_bytes += artifact.size_bytes
        return artifact

    def get_verified(self, handle: str) -> ReceiverArtifact:
        validate_artifact_handle(handle)
        with self._lock:
            artifact = self._artifacts.get(handle)
            if artifact is None:
                raise ReceiverArtifactNotFoundError("receiver artifact is unknown")
            if artifact.expires_at <= datetime.now(UTC):
                self._remove_locked(handle)
                raise ReceiverArtifactExpiredError("receiver artifact has expired")
            # The stored object is immutable; rechecking here prevents exposing
            # bytes if an internal programming error ever violated that invariant.
            if len(artifact.data) != artifact.size_bytes or not hmac.compare_digest(
                sha256(artifact.data).hexdigest(), artifact.checksum_sha256
            ):
                self._remove_locked(handle)
                raise ReceiverArtifactError("receiver artifact integrity check failed")
            return artifact

    def delete(self, handle: str) -> bool:
        validate_artifact_handle(handle)
        with self._lock:
            return self._remove_locked(handle)

    def cleanup(self) -> int:
        with self._lock:
            return self._cleanup_locked(datetime.now(UTC))

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return self._total_bytes

    @property
    def item_count(self) -> int:
        with self._lock:
            return len(self._artifacts)

    def _cleanup_locked(self, now: datetime) -> int:
        expired = [
            handle
            for handle, artifact in self._artifacts.items()
            if artifact.expires_at <= now
        ]
        for handle in expired:
            self._remove_locked(handle)
        return len(expired)

    def _require_capacity_locked(self, incoming_bytes: int) -> None:
        if (
            len(self._artifacts) >= self.config.max_items
            or self._total_bytes + incoming_bytes > self.config.max_total_bytes
        ):
            raise ReceiverArtifactError("receiver artifact cache is at its configured limit")

    def _remove_locked(self, handle: str) -> bool:
        artifact = self._artifacts.pop(handle, None)
        if artifact is None:
            return False
        self._total_bytes = max(0, self._total_bytes - artifact.size_bytes)
        return True


def validate_artifact_handle(value: str) -> str:
    if not _ARTIFACT_HANDLE.fullmatch(value):
        raise ReceiverArtifactNotFoundError("receiver artifact is unknown")
    return value


def validate_artifact_service_token(value: str | None, *, required: bool) -> str | None:
    if value is None or not value.strip():
        if required:
            raise TrafficDemoError(
                "a separate artifact-service token of at least 32 characters is required"
            )
        return None
    if len(value) < 32 or len(value) > 256 or any(character.isspace() for character in value):
        raise TrafficDemoError(
            "artifact-service token must be 32-256 non-whitespace characters"
        )
    return value
