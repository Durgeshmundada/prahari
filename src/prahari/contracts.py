"""Validated contracts shared by telemetry replay, analytics, and the API."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    """Base model that rejects unversioned or misspelled contract fields."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _normalise_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    return value.astimezone(UTC)


class RecordQuality(ContractModel):
    source_healthy: bool
    late_by_ms: int = Field(default=0, ge=0)
    is_estimated: bool = False
    counter_reset: bool = False


class MetricRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    record_type: Literal["metric"] = "metric"
    observed_at: datetime
    source: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)
    labels: dict[str, str] = Field(default_factory=dict)
    topology_version: str = Field(min_length=1)
    config_version: str | None = None
    quality: RecordQuality

    @field_validator("observed_at")
    @classmethod
    def normalise_observed_at(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)


class ChatMeasurements(ContractModel):
    """Application-message delivery and RTT evidence captured beside a transfer."""

    messages_attempted: int = Field(ge=1, le=100_000)
    messages_received: int = Field(ge=0, le=100_000)
    rtt_min_ms: float = Field(ge=0)
    rtt_p50_ms: float = Field(ge=0)
    rtt_p95_ms: float = Field(ge=0)
    rtt_max_ms: float = Field(ge=0)
    jitter_ms: float = Field(ge=0)
    message_loss_ratio: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_delivery_and_percentiles(self) -> ChatMeasurements:
        if self.messages_received > self.messages_attempted:
            raise ValueError("messages_received cannot exceed messages_attempted")
        if not self.rtt_min_ms <= self.rtt_p50_ms <= self.rtt_p95_ms <= self.rtt_max_ms:
            raise ValueError("chat RTT percentiles must be ordered from min through max")
        if (
            self.message_loss_ratio is not None
            and abs(self.message_loss_ratio - self.computed_message_loss_ratio) > 1e-6
        ):
            raise ValueError("message_loss_ratio must match message delivery counts")
        return self

    @property
    def computed_message_loss_ratio(self) -> float:
        return (self.messages_attempted - self.messages_received) / self.messages_attempted


class TransferObservation(ContractModel):
    """A bounded, application-reported result for one demo transfer.

    This lives inside an :class:`AppProbeRecord`, so the existing evidence store
    can retain the network measurements and the end-to-end transfer outcome
    together.  It intentionally contains metadata and checksums only--never a
    file body, URL, credential, or filesystem path.
    """

    transfer_id: str = Field(min_length=1, max_length=128)
    direction: Literal["branch_to_dc", "dc_to_branch"]
    protocol: Literal["websocket", "http"]
    content_type: str = Field(min_length=1, max_length=255)
    file_name: str | None = Field(default=None, max_length=255)
    kind: Literal["photo", "video", "file", "message"]
    bytes_expected: int = Field(ge=0, le=10_000_000_000)
    bytes_transferred: int = Field(ge=0, le=10_000_000_000)
    bytes_received: int | None = Field(default=None, ge=0, le=10_000_000_000)
    status: Literal["started", "completed", "failed", "cancelled"]
    checksum_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    integrity: Literal["pending", "verified", "failed"]
    error: str | None = Field(default=None, max_length=1_000)
    elapsed_ms: float | None = Field(default=None, ge=0, le=86_400_000)
    throughput_mbps: float | None = Field(default=None, ge=0, le=1_000_000)
    chat_measurements: ChatMeasurements | None = None

    @field_validator("file_name")
    @classmethod
    def reject_paths_in_file_name(cls, value: str | None) -> str | None:
        if value is not None and ("/" in value or "\\" in value or "\x00" in value):
            raise ValueError("file_name must be a name, not a path")
        return value

    @model_validator(mode="after")
    def validate_transfer_outcome(self) -> TransferObservation:
        if self.bytes_transferred > self.bytes_expected:
            raise ValueError("bytes_transferred cannot exceed bytes_expected")
        if self.bytes_received is not None and self.bytes_received > self.bytes_expected:
            raise ValueError("bytes_received cannot exceed bytes_expected")
        if self.status == "completed":
            if self.bytes_expected == 0:
                raise ValueError("a completed transfer must declare a non-zero expected size")
            if self.bytes_transferred != self.bytes_expected:
                raise ValueError("a completed transfer must send every expected byte")
            if self.bytes_received != self.bytes_expected:
                raise ValueError("a completed transfer must receive every expected byte")
            if self.integrity != "verified" or self.checksum_sha256 is None:
                raise ValueError("a completed transfer requires a verified SHA-256 checksum")
        if self.integrity == "verified" and self.status != "completed":
            raise ValueError("verified integrity is only valid for a completed transfer")
        if self.status == "failed" and self.integrity == "verified":
            raise ValueError("a failed transfer cannot claim verified integrity")
        return self


class AppProbeRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    record_type: Literal["app_probe"] = "app_probe"
    observed_at: datetime
    entity_id: str = Field(min_length=1)
    probe_name: str = Field(min_length=1)
    packet_loss_ratio: float = Field(ge=0, le=1)
    jitter_ms: float = Field(ge=0)
    request_latency_ms: float = Field(ge=0)
    path_id: str = Field(min_length=1)
    topology_version: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    quality: RecordQuality
    transfer: TransferObservation | None = None

    @field_validator("observed_at")
    @classmethod
    def normalise_observed_at(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)


class TrafficDemoEndpoint(ContractModel):
    """Named topology endpoint in the two-server application demo."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class TrafficDemoPayload(ContractModel):
    """Safe transfer metadata rendered by the dashboard; no file bytes are exposed."""

    name: str | None = Field(default=None, max_length=255)
    kind: Literal["photo", "video", "file", "message"] | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    bytes_sent: int | None = Field(default=None, ge=0)
    bytes_received: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    integrity: Literal["pending", "verified", "failed"] | None = None


class TrafficDemoLatency(ContractModel):
    min: float = Field(ge=0)
    p50: float = Field(ge=0)
    p95: float = Field(ge=0)
    max: float = Field(ge=0)


class TrafficDemoTraceStep(ContractModel):
    at: datetime | None = None
    stage: str = Field(min_length=1, max_length=100)
    status: Literal["pending", "active", "passed", "failed"]
    detail: str | None = Field(default=None, max_length=1_000)

    @field_validator("at")
    @classmethod
    def normalise_at(cls, value: datetime | None) -> datetime | None:
        return _normalise_timestamp(value) if value is not None else None


class TrafficDemoStatus(ContractModel):
    """Dashboard-facing summary derived from persisted transfer-enabled probes."""

    status: Literal["idle", "running", "passed", "failed", "unavailable"]
    can_run: bool = False
    max_file_bytes: int | None = Field(default=None, ge=1)
    updated_at: datetime | None = None
    path_id: str | None = Field(default=None, min_length=1)
    source: TrafficDemoEndpoint | None = None
    destination: TrafficDemoEndpoint | None = None
    transport: Literal["websocket", "http"] | None = None
    payload: TrafficDemoPayload | None = None
    latency_ms: TrafficDemoLatency | None = None
    throughput_mbps: float | None = Field(default=None, ge=0)
    loss_percent: float | None = Field(default=None, ge=0, le=100)
    trace: list[TrafficDemoTraceStep] = Field(default_factory=list, max_length=20)
    message: str | None = Field(default=None, max_length=1_000)


class TrafficDemoRunResponse(ContractModel):
    """Correlated outcome of one operator-triggered, development-only demo run."""

    run_id: str = Field(min_length=1, max_length=100)
    transfer_id: str = Field(min_length=1, max_length=100)
    evidence_saved: bool
    traffic: TrafficDemoStatus
    downloadable: bool = False
    download_url: str | None = Field(
        default=None,
        pattern=r"^/api/demo/traffic/files/file-[0-9a-f]{32}/download$",
    )
    download_expires_at: datetime | None = None

    @model_validator(mode="after")
    def require_consistent_download_fields(self) -> TrafficDemoRunResponse:
        has_download_fields = self.download_url is not None and self.download_expires_at is not None
        if self.downloadable != has_download_fields:
            raise ValueError(
                "downloadable must match the presence of download_url and download_expires_at"
            )
        return self


class EventRecord(ContractModel):
    schema_version: Literal["1.0"] = "1.0"
    record_type: Literal["event"] = "event"
    observed_at: datetime
    source: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    severity: Literal["info", "warning", "error", "critical"]
    message: str = Field(min_length=1, max_length=4_000)
    related_entity_ids: list[str] = Field(default_factory=list)
    topology_version: str = Field(min_length=1)
    config_version: str = Field(min_length=1)
    quality: RecordQuality

    @field_validator("observed_at")
    @classmethod
    def normalise_observed_at(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)


class MetricIngestRequest(ContractModel):
    run_id: str | None = Field(default=None, max_length=100)
    records: list[MetricRecord] = Field(min_length=1, max_length=5_000)


class AppProbeIngestRequest(ContractModel):
    run_id: str | None = Field(default=None, max_length=100)
    records: list[AppProbeRecord] = Field(min_length=1, max_length=5_000)


class EventIngestRequest(ContractModel):
    run_id: str | None = Field(default=None, max_length=100)
    records: list[EventRecord] = Field(min_length=1, max_length=5_000)


class IngestionResult(ContractModel):
    accepted: int = Field(ge=0)
    record_type: Literal["metric", "app_probe", "event"]
    run_id: str | None


class DataQuality(ContractModel):
    status: Literal["good", "degraded", "invalid"]
    reasons: list[str] = Field(default_factory=list)
    sample_counts: dict[str, int] = Field(default_factory=dict)
    newest_observation_at: datetime | None = None


class MetricWindowFeature(ContractModel):
    metric: str
    unit: str
    sample_count: int = Field(ge=0)
    latest: float
    mean: float
    p95: float
    stddev: float = Field(ge=0)
    slope_per_second: float
    ewma: float
    baseline_median: float
    robust_z: float


class FeatureWindow(ContractModel):
    feature_version: str
    entity_id: str
    window_start: datetime
    window_end: datetime
    data_quality: DataQuality
    metrics: dict[str, MetricWindowFeature]


class ImpactRisk(ContractModel):
    within_30_seconds: float = Field(ge=0, le=1)
    within_60_seconds: float = Field(ge=0, le=1)
    within_120_seconds: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def require_monotonic_horizons(self) -> ImpactRisk:
        if not (self.within_30_seconds <= self.within_60_seconds <= self.within_120_seconds):
            raise ValueError("risk must be nondecreasing as the prediction horizon grows")
        return self


class TimeToImpact(ContractModel):
    range: str = Field(min_length=1)
    basis: str = Field(min_length=1)


class RootLocationCandidate(ContractModel):
    entity_id: str
    site_id: str
    score: float = Field(ge=0, le=1)
    rationale: str


class AffectedScope(ContractModel):
    paths: list[str]
    applications: list[str]
    sites: list[str]
    backup_path_id: str | None = None


class Prediction(ContractModel):
    prediction_id: str
    model_version: str
    network_profile_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    model_bundle_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    entity_id: str
    likely_incident_type: Literal["normal", "tunnel_degradation"]
    incident_type_probabilities: dict[str, float]
    impact_risk: ImpactRisk
    time_to_impact: TimeToImpact
    root_location_candidates: list[RootLocationCandidate]
    affected_scope: AffectedScope
    confidence: float = Field(ge=0, le=1)
    risk_status: Literal["heuristic", "calibrated"]
    data_quality: Literal["good", "degraded", "invalid"]
    evidence: list[str]


class IncidentCard(ContractModel):
    id: str
    state: Literal[
        "watch",
        "predicted",
        "reviewed",
        "mitigating",
        "validating",
        "blocked",
        "resolved",
        "escalated",
    ]
    severity: Literal["informational", "warning", "high"]
    summary: str
    prediction: Prediction
    location: RootLocationCandidate
    data_quality: Literal["good", "degraded", "invalid"]
    evidence_ids: list[str]
    playbook_status: Literal["draft", "available_pending_review", "blocked"]


class AuditEvent(ContractModel):
    occurred_at: datetime
    actor_id: str
    event_type: str
    detail: str

    @field_validator("occurred_at")
    @classmethod
    def normalise_occurred_at(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)


class ReviewRequest(ContractModel):
    actor_id: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=1000)
    prediction_id: str | None = Field(default=None, min_length=1, max_length=100)


class ClosureRequest(ContractModel):
    actor_id: str = Field(min_length=1, max_length=100)
    prediction_id: str | None = Field(default=None, min_length=1, max_length=100)
    outcome: Literal["resolved", "partially_resolved", "failed", "false_positive", "uncertain"]
    note: str = Field(min_length=1, max_length=2000)
    intervention_at: datetime | None = None

    @field_validator("intervention_at")
    @classmethod
    def normalize_intervention_timestamp(cls, value: datetime | None) -> datetime | None:
        return _normalise_timestamp(value) if value is not None else None


class ClosureReviewRequest(ContractModel):
    actor_id: str = Field(min_length=1, max_length=100)
    decision: Literal["approved", "rejected"]
    note: str = Field(min_length=1, max_length=2000)


class RecoveryProbeWindow(ContractModel):
    window_start: datetime
    window_end: datetime
    samples: list[AppProbeRecord] = Field(min_length=2, max_length=100)
    packet_loss_ratio_max: float = Field(ge=0, le=1)
    jitter_ms_max: float = Field(ge=0)
    request_latency_ms_max: float = Field(ge=0)
    impact_detected: bool
    within_policy: bool

    @field_validator("window_start", "window_end")
    @classmethod
    def normalize_recovery_window_timestamp(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)

    @model_validator(mode="after")
    def validate_recovery_window(self) -> RecoveryProbeWindow:
        if self.window_end <= self.window_start:
            raise ValueError("recovery evidence window must have positive duration")
        if self.samples[0].observed_at != self.window_start:
            raise ValueError("recovery evidence start must match its first sample")
        if self.samples[-1].observed_at != self.window_end:
            raise ValueError("recovery evidence end must match its last sample")
        if any(
            current.observed_at > following.observed_at
            for current, following in zip(self.samples, self.samples[1:], strict=False)
        ):
            raise ValueError("recovery evidence samples must be time ordered")
        expected_maxima = (
            max(sample.packet_loss_ratio for sample in self.samples),
            max(sample.jitter_ms for sample in self.samples),
            max(sample.request_latency_ms for sample in self.samples),
        )
        recorded_maxima = (
            self.packet_loss_ratio_max,
            self.jitter_ms_max,
            self.request_latency_ms_max,
        )
        if recorded_maxima != expected_maxima:
            raise ValueError("recovery evidence maxima do not match its samples")
        if self.impact_detected and self.within_policy:
            raise ValueError("recovery evidence cannot be both impacted and within policy")
        return self


class RecoveryImpactThresholds(ContractModel):
    packet_loss_ratio: float = Field(ge=0, le=1)
    jitter_ms: float = Field(ge=0)


def recovery_evidence_sha256(
    *,
    schema_version: Literal["recovery-comparison-v1", "recovery-comparison-v2"],
    path_id: str,
    intervention_at: datetime,
    impact_policy_version: str,
    impact_thresholds: RecoveryImpactThresholds | None,
    required_sustained_seconds: int,
    before: RecoveryProbeWindow,
    after: RecoveryProbeWindow,
    recovered: bool,
) -> str:
    """Hash the canonical recovery evidence for its declared schema version."""

    if schema_version == "recovery-comparison-v1":
        evidence: dict[str, object] = {
            "schema_version": schema_version,
            "path_id": path_id,
            "intervention_at": intervention_at.isoformat(),
            "before": [sample.model_dump(mode="json") for sample in before.samples],
            "after": [sample.model_dump(mode="json") for sample in after.samples],
        }
    else:
        if impact_thresholds is None:
            raise ValueError("v2 recovery evidence requires captured impact thresholds")
        evidence = {
            "schema_version": schema_version,
            "path_id": path_id,
            "intervention_at": intervention_at.isoformat(),
            "impact_policy_version": impact_policy_version,
            "impact_thresholds": impact_thresholds.model_dump(mode="json"),
            "required_sustained_seconds": required_sustained_seconds,
            "before": before.model_dump(mode="json"),
            "after": after.model_dump(mode="json"),
            "recovered": recovered,
        }
    return hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RecoveryComparison(ContractModel):
    schema_version: Literal["recovery-comparison-v1", "recovery-comparison-v2"] = (
        "recovery-comparison-v2"
    )
    path_id: str = Field(min_length=1)
    intervention_at: datetime
    impact_policy_version: str = Field(min_length=1)
    impact_thresholds: RecoveryImpactThresholds | None = None
    required_sustained_seconds: int = Field(ge=1)
    before: RecoveryProbeWindow
    after: RecoveryProbeWindow
    recovered: bool
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("intervention_at")
    @classmethod
    def normalize_recovery_timestamp(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)

    @model_validator(mode="after")
    def validate_recovery_comparison(self) -> RecoveryComparison:
        if self.before.window_end >= self.intervention_at:
            raise ValueError("pre-intervention evidence must precede the intervention")
        if self.after.window_start < self.intervention_at:
            raise ValueError("post-intervention evidence cannot precede the intervention")
        if any(
            sample.path_id != self.path_id
            for sample in [*self.before.samples, *self.after.samples]
        ):
            raise ValueError("recovery evidence samples must match the compared path")
        if self.recovered != (self.before.impact_detected and self.after.within_policy):
            raise ValueError("recovery verdict does not match the evidence windows")
        if self.schema_version == "recovery-comparison-v2":
            if self.impact_thresholds is None:
                raise ValueError("v2 recovery evidence requires captured impact thresholds")
            for label, window in (("before", self.before), ("after", self.after)):
                breaches = [
                    sample.packet_loss_ratio >= self.impact_thresholds.packet_loss_ratio
                    or sample.jitter_ms >= self.impact_thresholds.jitter_ms
                    for sample in window.samples
                ]
                if window.impact_detected != all(breaches):
                    raise ValueError(f"{label} impact verdict does not match captured thresholds")
                if window.within_policy != (not any(breaches)):
                    raise ValueError(f"{label} policy verdict does not match captured thresholds")
        if (
            (self.before.window_end - self.before.window_start).total_seconds()
            < self.required_sustained_seconds
            or (self.after.window_end - self.after.window_start).total_seconds()
            < self.required_sustained_seconds
        ):
            raise ValueError("recovery evidence does not cover the required sustained duration")
        expected = recovery_evidence_sha256(
            schema_version=self.schema_version,
            path_id=self.path_id,
            intervention_at=self.intervention_at,
            impact_policy_version=self.impact_policy_version,
            impact_thresholds=self.impact_thresholds,
            required_sustained_seconds=self.required_sustained_seconds,
            before=self.before,
            after=self.after,
            recovered=self.recovered,
        )
        if self.evidence_sha256 != expected:
            raise ValueError("recovery evidence SHA-256 does not match its captured evidence")
        return self


class ClosureCase(ContractModel):
    id: str
    incident_id: str
    prediction_id: str
    created_at: datetime
    submission: ClosureRequest
    incident: IncidentCard
    feature_window: FeatureWindow
    app_impact_started: bool
    recovery_comparison: RecoveryComparison | None = None
    review: ClosureReviewRequest | None = None
    reviewed_at: datetime | None = None


class ActionResult(ContractModel):
    playbook_id: str
    status: Literal[
        "blocked",
        "dry_run_passed",
        "applied",
        "rolled_back",
        "validation_failed",
    ]
    message: str
    audit_event: AuditEvent


class ActionAgentReceipt(ContractModel):
    operation_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9_.:@-]+$",
    )
    status: Literal["dry_run_passed", "applied", "rolled_back", "blocked", "validation_failed"]
    message: str = Field(min_length=1, max_length=2000)
    observed_route: Literal["primary", "backup", "unknown"]
    checked_at: datetime

    @field_validator("checked_at")
    @classmethod
    def normalize_receipt_timestamp(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)


class ActionReconciliationRequest(ContractModel):
    actor_id: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=20, max_length=2000)
    evidence_reference: str = Field(min_length=3, max_length=1000)
    evidence_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dispatch_quiesced: Literal[True]
    observed_route: Literal["primary", "backup"]

    @field_validator("evidence_sha256")
    @classmethod
    def reject_placeholder_checksum(cls, value: str) -> str:
        if value == "0" * 64:
            raise ValueError("reconciliation requires a non-placeholder evidence checksum")
        return value


class ActionOperation(ContractModel):
    id: str
    incident_id: str
    playbook_id: str
    prediction_id: str
    prediction_snapshot: Prediction
    kind: Literal["apply", "rollback"]
    actor_id: str
    note: str
    created_at: datetime
    updated_at: datetime
    status: Literal["pending", "unknown", "finished"]
    result_status: Literal["applied", "rolled_back", "blocked", "validation_failed"] | None = None
    observed_route: Literal["primary", "backup", "unknown"] | None = None
    message: str | None = None
    reconciliation: ActionReconciliationRequest | None = None
    agent_receipt: ActionAgentReceipt | None = None

    @field_validator("created_at", "updated_at")
    @classmethod
    def normalize_operation_timestamp(cls, value: datetime) -> datetime:
        return _normalise_timestamp(value)

    @model_validator(mode="after")
    def validate_operation_evidence(self) -> ActionOperation:
        if self.prediction_snapshot.prediction_id != self.prediction_id:
            raise ValueError("action snapshot does not match its prediction")
        if self.updated_at < self.created_at:
            raise ValueError("action update cannot precede its creation")
        if self.status != "finished" and self.reconciliation is not None:
            raise ValueError("unconfirmed operations cannot contain final reconciliation")
        if self.status == "finished" and self.result_status is None and self.reconciliation is None:
            raise ValueError("finished operation requires a result or independent reconciliation")
        if self.status == "pending" and any(
            value is not None
            for value in (
                self.result_status,
                self.observed_route,
                self.message,
                self.agent_receipt,
            )
        ):
            raise ValueError("pending operation cannot contain outcome evidence")
        if self.status == "unknown" and self.result_status not in {None, "validation_failed"}:
            raise ValueError("unknown operation cannot claim a confirmed result")
        if self.status == "finished" and self.reconciliation is None and self.result_status not in {
            "applied",
            "rolled_back",
            "blocked",
        }:
            raise ValueError("finished adapter operation requires a terminal adapter result")
        if self.result_status == "applied" and (
            self.kind != "apply" or self.observed_route != "backup"
        ):
            raise ValueError("applied operation requires apply kind and observed backup route")
        if self.result_status == "rolled_back" and (
            self.kind != "rollback" or self.observed_route != "primary"
        ):
            raise ValueError("rolled-back operation requires rollback kind and primary route")
        if (
            self.agent_receipt is not None
            and self.result_status is not None
            and self.agent_receipt.status != self.result_status
        ):
            raise ValueError("action result does not match its original adapter receipt")
        if (
            self.agent_receipt is not None
            and self.agent_receipt.operation_id is not None
            and self.agent_receipt.operation_id != self.id
        ):
            raise ValueError("action receipt does not match its operation ID")
        if self.reconciliation is not None:
            if self.reconciliation.actor_id == self.actor_id:
                raise ValueError("action reconciliation requires an independent reviewer")
            if self.observed_route != self.reconciliation.observed_route:
                raise ValueError("reconciled route does not match its evidence")
        return self


class ActionOperationsPage(ContractModel):
    operations: list[ActionOperation]
    dispatch_enabled: bool
