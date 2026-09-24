"""Validate release-bound acceptance records without claiming certification."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field, field_validator, model_validator

from prahari.contracts import ContractModel

BASELINE_REQUIREMENTS = frozenset(
    {
        "network_collection",
        "model_validation",
        "backup_restore",
        "capacity_failure",
        "browser_accessibility",
        "credential_rotation",
        "network_isolation",
        "vulnerability_scan",
        "independent_security_review",
        "operating_ownership",
    }
)


class AcceptanceEvidence(ContractModel):
    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def safe_reference(self):
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or "\\" in self.path
            or ":" in self.path
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
        ):
            raise ValueError("acceptance evidence requires a safe relative path")
        if self.sha256 == "0" * 64:
            raise ValueError("acceptance evidence cannot use placeholder checksums")
        return self


class AcceptanceRequirement(ContractModel):
    requirement_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,99}$")
    status: Literal["pending", "passed", "failed"]
    note: str = Field(min_length=20, max_length=2000)
    evidence: list[AcceptanceEvidence] = Field(min_length=1, max_length=20)

    @field_validator("note")
    @classmethod
    def substantive_note(cls, value):
        if len(value.strip()) < 20:
            raise ValueError("acceptance requirement requires a substantive verification note")
        return value.strip()


class ReleaseAcceptance(ContractModel):
    schema_version: Literal["release-acceptance-v1"] = "release-acceptance-v1"
    deployment_mode: Literal["monitoring_only"]
    release_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    customer_requirement_references: list[str] = Field(min_length=1, max_length=50)
    prepared_by: str = Field(min_length=1, max_length=100)
    approved_by: str = Field(min_length=1, max_length=100)
    approval_reference: str = Field(min_length=3, max_length=1000)
    approved_at: datetime
    expires_at: datetime
    requirements: list[AcceptanceRequirement] = Field(min_length=10, max_length=100)

    @field_validator("prepared_by", "approved_by", "approval_reference")
    @classmethod
    def nonblank_approval(cls, value):
        if not value.strip():
            raise ValueError("acceptance approval identities and reference must not be blank")
        return value.strip()

    @field_validator("approved_at", "expires_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("acceptance timestamps require a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def approval_and_coverage(self):
        if self.prepared_by.strip().casefold() == self.approved_by.strip().casefold():
            raise ValueError("acceptance requires an independent approver")
        if any(
            not 3 <= len(reference.strip()) <= 1000
            for reference in self.customer_requirement_references
        ):
            raise ValueError("customer requirement references must contain 3-1000 characters")
        ids = [item.requirement_id for item in self.requirements]
        if len(ids) != len(set(ids)) or not BASELINE_REQUIREMENTS <= set(ids):
            raise ValueError("acceptance must cover the complete baseline without duplicate IDs")
        if self.release_sha256 == "0" * 64:
            raise ValueError("acceptance cannot use a placeholder release identity")
        return self


def validate_acceptance(
    report: ReleaseAcceptance,
    evidence_root: Path,
    *,
    release_sha256: str,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    if report.release_sha256 != release_sha256:
        raise ValueError("acceptance belongs to a different release")
    if (
        report.approved_at > now
        or report.expires_at <= now
        or report.expires_at <= report.approved_at
    ):
        raise ValueError("acceptance approval is future-dated or expired")
    if any(requirement.status != "passed" for requirement in report.requirements):
        raise ValueError("release acceptance contains pending or failed requirements")
    root = evidence_root.absolute()
    for parent in [root, *root.parents]:
        if parent.is_symlink():
            raise ValueError("acceptance evidence root must not traverse symlinks")
    if not root.is_dir():
        raise ValueError("acceptance evidence root must be an existing directory")
    checked = set()
    for requirement in report.requirements:
        for artifact in requirement.evidence:
            identity = (artifact.path, artifact.sha256)
            if identity in checked:
                continue
            file = root / artifact.path
            for parent in [file, *file.parents]:
                if parent.is_symlink():
                    raise ValueError("acceptance evidence must not traverse symlinks")
            if not file.resolve().is_relative_to(root.resolve()) or not file.is_file():
                raise ValueError("acceptance evidence file is missing or outside its root")
            digest = sha256()
            total = 0
            with file.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    total += len(chunk)
                    if total > 64 * 1024 * 1024:
                        raise ValueError("acceptance evidence exceeds the 64 MiB artifact limit")
                    digest.update(chunk)
            if total == 0 or digest.hexdigest() != artifact.sha256:
                raise ValueError("acceptance evidence is empty or its checksum changed")
            checked.add(identity)
