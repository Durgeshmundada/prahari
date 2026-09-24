"""Shared contracts, impact labels, and rolling features for predictive Track A v2."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from math import exp, log
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from prahari.analytics.features import calculate_metric_feature

FEATURE_VERSION = "predictive-window-v2"
MAX_MEASUREMENT_JOIN_SKEW_SECONDS = 10.0
MINIMUM_BASELINE_SAMPLES = 12
MINIMUM_RECOVERY_SAMPLES = 12
MINIMUM_APPLIED_FAULT_STEPS = 6
FEATURE_METRICS: dict[str, tuple[str, Callable[[PredictivePoint], float]]] = {
    "network_packet_loss_ratio": ("ratio", lambda point: point.network.packet_loss_ratio),
    "network_jitter_ms": ("ms", lambda point: point.network.jitter_ms),
    "network_rtt_ms": ("ms", lambda point: point.network.rtt_avg_ms),
    "app_message_loss_ratio": (
        "ratio",
        lambda point: point.application.message_loss_ratio,
    ),
    "app_rtt_p95_ms": ("ms", lambda point: point.application.rtt_p95_ms),
    "app_jitter_ms": ("ms", lambda point: point.application.jitter_ms),
    "app_upload_failure_ratio": (
        "ratio",
        lambda point: 0.0 if point.application.upload_status == "completed" else 1.0,
    ),
}
STATISTIC_FIELDS = (
    "latest",
    "mean",
    "p95",
    "stddev",
    "slope_per_second",
    "ewma",
    "baseline_median",
    "robust_z",
)
FEATURE_NAMES = tuple(
    f"{metric}__{statistic}" for metric in FEATURE_METRICS for statistic in STATISTIC_FIELDS
)


def parse_strict_json(value: str | bytes) -> object:
    """Decode evidence JSON while rejecting duplicate keys and non-finite numbers."""

    def build_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"JSON contains duplicate key: {key}")
            result[key] = item
        return result

    def reject_constant(constant: str) -> object:
        raise ValueError(f"JSON contains non-finite number: {constant}")

    return json.loads(
        value,
        object_pairs_hook=build_object,
        parse_constant=reject_constant,
    )


def calibrate_probability(probability: float, slope: float, intercept: float) -> float:
    """Apply the exact persisted Platt calibration used by training and runtime."""

    if not 0 <= probability <= 1:
        raise ValueError("uncalibrated probability must be between zero and one")
    if slope <= 0:
        raise ValueError("calibration slope must be positive")
    clipped = min(1 - 1e-6, max(1e-6, probability))
    logit = log(clipped / (1 - clipped))
    value = max(-35.0, min(35.0, slope * logit + intercept))
    return 1 / (1 + exp(-value))


def monotonic_horizon_probabilities(probabilities: Sequence[float]) -> list[float]:
    """Project horizon risks onto the required non-decreasing contract."""

    if len(probabilities) != 3:
        raise ValueError("exactly three horizon probabilities are required")
    monotonic: list[float] = []
    for probability in probabilities:
        if not 0 <= probability <= 1:
            raise ValueError("horizon probabilities must be between zero and one")
        monotonic.append(max(monotonic[-1] if monotonic else 0.0, probability))
    return monotonic


class PredictiveContract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class NetworkMeasurement(PredictiveContract):
    sent: int | None = Field(default=None, ge=3, le=100)
    received: int | None = Field(default=None, ge=0, le=100)
    packet_loss_ratio: float = Field(ge=0, le=1)
    rtt_avg_ms: float = Field(ge=0)
    jitter_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts_and_loss(self) -> NetworkMeasurement:
        if (self.sent is None) != (self.received is None):
            raise ValueError("network sent and received counts must be present together")
        if self.sent is None or self.received is None:
            return self
        if self.received > self.sent:
            raise ValueError("network received count cannot exceed sent count")
        expected_loss = (self.sent - self.received) / self.sent
        if abs(self.packet_loss_ratio - expected_loss) > 0.000_001:
            raise ValueError("network loss ratio does not match sent and received counts")
        if self.received == 0 and (self.rtt_avg_ms != 0 or self.jitter_ms != 0):
            raise ValueError("a fully lost network probe cannot report RTT or jitter")
        return self


class ApplicationMeasurement(PredictiveContract):
    status: Literal["passed", "failed"]
    messages_attempted: int = Field(ge=1, le=100)
    messages_received: int = Field(ge=0, le=100)
    message_loss_ratio: float = Field(ge=0, le=1)
    rtt_p95_ms: float = Field(ge=0)
    jitter_ms: float = Field(ge=0)
    upload_status: Literal["completed", "failed"]
    upload_integrity: Literal["verified", "failed"]
    upload_throughput_mbps: float = Field(ge=0)
    evidence_saved: bool

    @model_validator(mode="after")
    def validate_application_measurement(self) -> ApplicationMeasurement:
        if self.messages_received > self.messages_attempted:
            raise ValueError("application received count cannot exceed attempted count")
        expected_loss = (self.messages_attempted - self.messages_received) / self.messages_attempted
        if abs(self.message_loss_ratio - expected_loss) > 0.000_001:
            raise ValueError("application loss ratio does not match message counts")
        upload_passed = self.upload_status == "completed" and self.upload_integrity == "verified"
        expected_status = "passed" if self.message_loss_ratio == 0 and upload_passed else "failed"
        if self.status != expected_status:
            raise ValueError("application status conflicts with measured message/upload result")
        return self


class InjectedCondition(PredictiveContract):
    loss_percent: float = Field(ge=0, le=20)
    delay_ms: float = Field(ge=0, le=250)
    jitter_ms: float = Field(ge=0, le=100)


class PredictivePoint(PredictiveContract):
    observed_at: datetime
    network: NetworkMeasurement
    application: ApplicationMeasurement

    @field_validator("observed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a UTC offset")
        return value.astimezone(UTC)


class PredictiveObservation(PredictivePoint):
    schema_version: Literal["predictive-observation-v2"] = "predictive-observation-v2"
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$")
    controller_lab_name: Literal["prahari", "prahari-owned"] = "prahari"
    sequence: int = Field(ge=0)
    scenario: Literal["normal", "gradual_loss"]
    phase: Literal["baseline", "ramp", "steady", "recovery"]
    target_entity_id: Literal["interface:branch-edge:eth1"]
    injected: InjectedCondition
    probe_started_at: datetime
    network_observed_at: datetime
    application_observed_at: datetime
    topology_version: Literal["lab-v1"]
    config_version: Literal["lab-config-v1"]
    local_evidence_complete: bool
    network_records_persisted: bool
    application_records_persisted: bool
    network_error: str | None = Field(default=None, max_length=500)
    application_error: str | None = Field(default=None, max_length=500)

    @field_validator("probe_started_at", "network_observed_at", "application_observed_at")
    @classmethod
    def normalize_component_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("component observation timestamps must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_raw_probe_counts_and_aligned_times(self) -> PredictiveObservation:
        if self.network.sent is None or self.network.received is None:
            raise ValueError("raw predictive observations require network packet counts")
        if self.observed_at != self.application_observed_at:
            raise ValueError("predictive observed_at must equal application_observed_at")
        if not (self.probe_started_at <= self.network_observed_at <= self.application_observed_at):
            raise ValueError("predictive component timestamps are not causal")
        join_skew = (self.application_observed_at - self.network_observed_at).total_seconds()
        if not 0 <= join_skew <= MAX_MEASUREMENT_JOIN_SKEW_SECONDS:
            raise ValueError(
                "network and application measurements must be causal and within the join ceiling"
            )
        return self


class PredictiveFaultRecord(PredictiveContract):
    """One durable controller transition in a gradual-loss experiment."""

    schema_version: Literal["predictive-fault-step-v2"] = "predictive-fault-step-v2"
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$")
    state: Literal["planned", "applied", "clear_planned", "cleared"]
    recorded_at: datetime
    target: Literal["clab-prahari-branch-edge:eth1", "clab-prahari-owned-branch-edge:eth1"]
    sequence: int = Field(ge=0)
    condition: InjectedCondition
    qdisc_snapshot: list[dict[str, object]] | None = None

    @field_validator("recorded_at")
    @classmethod
    def normalize_fault_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fault-record timestamp must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_transition_shape(self) -> PredictiveFaultRecord:
        is_acknowledgement = self.state in {"applied", "cleared"}
        if is_acknowledgement != (self.qdisc_snapshot is not None):
            raise ValueError(
                "only applied/cleared records may contain the post-command qdisc snapshot"
            )
        if self.state in {"clear_planned", "cleared"} and self.condition != InjectedCondition(
            loss_percent=0,
            delay_ms=0,
            jitter_ms=0,
        ):
            raise ValueError("clear transitions must declare the zero injected condition")
        return self


class ApplicationImpactPolicy(PredictiveContract):
    schema_version: Literal["application-impact-policy-v2"] = "application-impact-policy-v2"
    version: str = Field(min_length=1, max_length=100)
    sustained_seconds: int = Field(ge=5, le=300)
    max_inter_sample_gap_seconds: int = Field(ge=5, le=60)
    message_loss_ratio: float = Field(gt=0, le=1)
    rtt_p95_ms: float = Field(gt=0)
    jitter_ms: float = Field(gt=0)
    failed_upload_is_impact: bool = True


class ImpactDetection(PredictiveContract):
    actual_impact_start_time: datetime | None
    confirmed_at: datetime | None
    triggering_signals: list[str]


class EvidenceFile(PredictiveContract):
    path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{1,199}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("path")
    @classmethod
    def require_safe_relative_path(cls, value: str) -> str:
        if value.startswith("/") or ".." in value.split("/"):
            raise ValueError("evidence path must be a safe relative path")
        return value


class PredictiveModelApproval(PredictiveContract):
    schema_version: Literal["predictive-model-approval-v2"]
    decision: Literal["approved"]
    model_version: str = Field(min_length=1, max_length=100)
    dataset_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    dataset_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    candidate_manifest_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    deployment_scope: Literal["controlled_lab", "production"]
    reviewer_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._@-]{2,99}$")
    reviewed_at: datetime
    rationale: str = Field(min_length=20, max_length=2_000)
    technical_gate_reviewed: Literal[True]
    untouched_holdout_reviewed: Literal[True]
    limitations_accepted: Literal[True]
    external_validation_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^[a-f0-9]{64}$",
    )
    change_ticket: str | None = Field(default=None, min_length=3, max_length=100)
    network_profile_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @field_validator("reviewed_at")
    @classmethod
    def normalize_reviewed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approval review time must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_production_evidence(self) -> PredictiveModelApproval:
        if self.deployment_scope == "production" and (
            self.external_validation_evidence_sha256 is None or self.change_ticket is None
        ):
            raise ValueError(
                "production approval requires external validation evidence and a change ticket"
            )
        if self.deployment_scope == "production" and self.network_profile_sha256 is None:
            raise ValueError("production approval requires a network profile checksum")
        return self


class PredictiveExperimentManifest(PredictiveContract):
    schema_version: Literal["predictive-experiment-manifest-v2"]
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$")
    controller_lab_name: Literal["prahari", "prahari-owned"] = "prahari"
    status: Literal["completed"]
    scenario: Literal["normal", "gradual_loss"]
    experiment_seed: int = Field(ge=0, le=2_147_483_647)
    traffic_profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$")
    fault_profile_id: Literal["none", "a", "b", "c"]
    started_at: datetime
    completed_at: datetime
    target_entity_id: Literal["interface:branch-edge:eth1"]
    topology_version: Literal["lab-v1"]
    config_version: Literal["lab-config-v1"]
    sampling_seconds: int = Field(ge=1, le=30)
    window_seconds: Literal[60]
    horizons_seconds: tuple[Literal[30], Literal[60], Literal[120]]
    sample_count: int = Field(ge=1)
    observations_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    impact_policy: ApplicationImpactPolicy
    actual_impact_start_time: datetime | None
    impact_confirmed_at: datetime | None
    impact_triggering_signals: list[str]
    root_cause_verification: Literal["controlled_fault_manifest", "not_applicable"]
    fault_manifests: list[EvidenceFile]

    @field_validator(
        "started_at",
        "completed_at",
        "actual_impact_start_time",
        "impact_confirmed_at",
    )
    @classmethod
    def normalize_manifest_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("manifest timestamps must include a UTC offset")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_manifest_semantics(self) -> PredictiveExperimentManifest:
        if self.completed_at <= self.started_at:
            raise ValueError("experiment completion must follow its start")
        if (self.actual_impact_start_time is None) != (self.impact_confirmed_at is None):
            raise ValueError("impact start and confirmation must both be present or absent")
        if self.actual_impact_start_time is not None:
            if not self.actual_impact_start_time <= self.impact_confirmed_at <= self.completed_at:
                raise ValueError("impact timestamps are outside the experiment interval")
            if not self.impact_triggering_signals:
                raise ValueError("an observed impact requires triggering application signals")
        elif self.impact_triggering_signals:
            raise ValueError("impact signals cannot be declared without observed impact")
        if self.scenario == "normal":
            if self.actual_impact_start_time is not None:
                raise ValueError(
                    "a normal run with observed impact is not eligible as a normal label"
                )
            if self.root_cause_verification != "not_applicable" or self.fault_manifests:
                raise ValueError("a normal run cannot declare an injected root cause")
            if self.fault_profile_id != "none":
                raise ValueError("a normal run must use the none fault profile")
        elif (
            self.root_cause_verification != "controlled_fault_manifest" or not self.fault_manifests
        ):
            raise ValueError("a gradual-loss run requires controlled fault-manifest evidence")
        elif self.fault_profile_id == "none":
            raise ValueError("a gradual-loss run must name its fault profile")
        return self


class PredictiveFeatureVector(PredictiveContract):
    feature_version: Literal["predictive-window-v2"] = FEATURE_VERSION
    run_id: str | None = None
    window_start: datetime
    window_end: datetime
    sample_count: int = Field(ge=1)
    values: dict[str, float]

    @model_validator(mode="after")
    def validate_feature_contract(self) -> PredictiveFeatureVector:
        if tuple(self.values) != FEATURE_NAMES:
            raise ValueError("predictive feature names or ordering do not match the contract")
        return self


def application_impact_signals(
    measurement: ApplicationMeasurement,
    policy: ApplicationImpactPolicy,
) -> list[str]:
    """Return only application-observed impact signals; injection phase is never a label."""

    signals: list[str] = []
    if measurement.message_loss_ratio >= policy.message_loss_ratio:
        signals.append("application_message_loss")
    if measurement.rtt_p95_ms >= policy.rtt_p95_ms:
        signals.append("application_rtt")
    if measurement.jitter_ms >= policy.jitter_ms:
        signals.append("application_jitter")
    if policy.failed_upload_is_impact and (
        measurement.upload_status != "completed" or measurement.upload_integrity != "verified"
    ):
        signals.append("application_upload_failure")
    return signals


def detect_sustained_application_impact(
    observations: Sequence[PredictivePoint],
    policy: ApplicationImpactPolicy,
) -> ImpactDetection:
    """Detect the first sustained application breach from measured probe results."""

    ordered = _validated_order(observations)
    streak_start: datetime | None = None
    streak_signals: set[str] = set()
    previous_at: datetime | None = None
    for observation in ordered:
        signals = application_impact_signals(observation.application, policy)
        if not signals or (
            previous_at is not None
            and (observation.observed_at - previous_at).total_seconds()
            > policy.max_inter_sample_gap_seconds
        ):
            streak_start = None
            streak_signals.clear()
        if signals:
            if streak_start is None:
                streak_start = observation.observed_at
            streak_signals.update(signals)
            if (observation.observed_at - streak_start).total_seconds() >= policy.sustained_seconds:
                return ImpactDetection(
                    actual_impact_start_time=streak_start,
                    confirmed_at=observation.observed_at,
                    triggering_signals=sorted(streak_signals),
                )
        previous_at = observation.observed_at
    return ImpactDetection(
        actual_impact_start_time=None,
        confirmed_at=None,
        triggering_signals=[],
    )


def extract_predictive_feature_vectors(
    observations: Sequence[PredictivePoint],
    *,
    run_id: str | None,
    window_seconds: int = 60,
    sampling_seconds: int = 5,
    minimum_samples: int = 10,
    max_inter_sample_gap_seconds: int = 15,
) -> list[PredictiveFeatureVector]:
    """Build deterministic 60-second rolling vectors for both training and runtime."""

    if window_seconds <= sampling_seconds:
        raise ValueError("window_seconds must be greater than sampling_seconds")
    if minimum_samples < 2:
        raise ValueError("minimum_samples must be at least two")
    ordered = _validated_order(observations)
    vectors: list[PredictiveFeatureVector] = []
    minimum_span = window_seconds - sampling_seconds
    for end_index, current in enumerate(ordered):
        lower_bound = current.observed_at - timedelta(seconds=window_seconds)
        window = [point for point in ordered[: end_index + 1] if point.observed_at >= lower_bound]
        if len(window) < minimum_samples:
            continue
        if (window[-1].observed_at - window[0].observed_at).total_seconds() < minimum_span:
            continue
        if any(
            (later.observed_at - earlier.observed_at).total_seconds() > max_inter_sample_gap_seconds
            for earlier, later in zip(window, window[1:], strict=False)
        ):
            continue
        values: dict[str, float] = {}
        for metric, (unit, value_for) in FEATURE_METRICS.items():
            feature = calculate_metric_feature(
                metric,
                unit,
                [(point.observed_at, value_for(point)) for point in window],
            )
            for statistic in STATISTIC_FIELDS:
                values[f"{metric}__{statistic}"] = float(getattr(feature, statistic))
        vectors.append(
            PredictiveFeatureVector(
                run_id=run_id,
                window_start=window[0].observed_at,
                window_end=window[-1].observed_at,
                sample_count=len(window),
                values=values,
            )
        )
    return vectors


def _validated_order(
    observations: Sequence[PredictivePoint],
) -> list[PredictivePoint]:
    if not observations:
        raise ValueError("at least one predictive observation is required")
    ordered = sorted(observations, key=lambda observation: observation.observed_at)
    if any(
        later.observed_at <= earlier.observed_at
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ):
        raise ValueError("predictive observations must have unique timestamps")
    return ordered


def validate_fault_schedule(
    records: Sequence[PredictiveFaultRecord],
    observations: Sequence[PredictiveObservation],
    *,
    run_id: str,
) -> None:
    """Prove controller transitions and per-sample injected conditions agree causally."""

    if not records:
        raise ValueError("fault schedule cannot be empty")
    if not observations:
        raise ValueError("fault schedule requires observations")
    if any(record.run_id != run_id for record in records):
        raise ValueError("fault schedule contains a foreign run ID")
    if any(observation.run_id != run_id for observation in observations):
        raise ValueError("fault schedule observations contain a foreign run ID")
    labs = {observation.controller_lab_name for observation in observations}
    if len(labs) != 1:
        raise ValueError("fault schedule observations contain different controller labs")
    expected_target = f"clab-{next(iter(labs))}-branch-edge:eth1"
    if any(record.target != expected_target for record in records):
        raise ValueError("fault schedule target does not match the observation controller lab")
    if any(observation.scenario != "gradual_loss" for observation in observations):
        raise ValueError("fault schedule can only bind gradual-loss observations")
    if [observation.sequence for observation in observations] != list(range(len(observations))):
        raise ValueError("fault schedule requires a contiguous observation sequence")
    if any(
        later.recorded_at < earlier.recorded_at
        for earlier, later in zip(records, records[1:], strict=False)
    ):
        raise ValueError("fault schedule records are not chronological")

    transitions: list[tuple[PredictiveFaultRecord, PredictiveFaultRecord]] = []
    position = 0
    previous_sequence = -1
    clear_seen = False
    while position < len(records):
        if position + 1 >= len(records):
            raise ValueError("fault schedule ends with an unacknowledged transition")
        planned = records[position]
        acknowledged = records[position + 1]
        expected_acknowledgement = {
            "planned": "applied",
            "clear_planned": "cleared",
        }.get(planned.state)
        if expected_acknowledgement is None or acknowledged.state != expected_acknowledgement:
            raise ValueError("fault schedule must alternate planned and acknowledged records")
        if planned.sequence != acknowledged.sequence:
            raise ValueError("fault transition acknowledgement names a different sequence")
        if planned.sequence <= previous_sequence:
            raise ValueError("fault transition sequences must be strictly increasing")
        if planned.sequence >= len(observations):
            raise ValueError("fault transition sequence has no corresponding observation")
        if planned.condition != acknowledged.condition:
            raise ValueError("planned and acknowledged fault conditions differ")
        if planned.recorded_at > acknowledged.recorded_at:
            raise ValueError("fault transition was acknowledged before it was planned")
        if planned.sequence > 0 and (
            observations[planned.sequence - 1].application_observed_at > planned.recorded_at
        ):
            raise ValueError("a fault transition overlaps the preceding application probe")
        if acknowledged.recorded_at > observations[planned.sequence].probe_started_at:
            raise ValueError("an observation began before its fault transition was acknowledged")
        if planned.state == "clear_planned":
            if clear_seen:
                raise ValueError("fault schedule contains more than one clear transition")
            clear_seen = True
            if position + 2 != len(records):
                raise ValueError("the clear transition must be the final controller transition")
            _validate_cleared_qdisc(acknowledged)
        else:
            if clear_seen:
                raise ValueError("a fault cannot be applied after the clear transition")
            _validate_applied_qdisc(acknowledged)
        transitions.append((planned, acknowledged))
        previous_sequence = planned.sequence
        position += 2

    applied_transitions = [pair for pair in transitions if pair[0].state == "planned"]
    clear_transitions = [pair for pair in transitions if pair[0].state == "clear_planned"]
    if not applied_transitions or len(clear_transitions) != 1:
        raise ValueError("fault schedule needs applied fault steps followed by one clear")
    if len(applied_transitions) < MINIMUM_APPLIED_FAULT_STEPS:
        raise ValueError(
            "a gradual-loss schedule requires at least "
            f"{MINIMUM_APPLIED_FAULT_STEPS} independently acknowledged fault steps"
        )
    first_fault_sequence = applied_transitions[0][0].sequence
    clear_sequence = clear_transitions[0][0].sequence
    if first_fault_sequence < MINIMUM_BASELINE_SAMPLES:
        raise ValueError(
            "a gradual-loss run needs at least "
            f"{MINIMUM_BASELINE_SAMPLES} observed zero-condition baseline samples"
        )
    if clear_sequence <= applied_transitions[-1][0].sequence:
        raise ValueError("fault clear must follow every applied fault step")
    if len(observations) - clear_sequence < MINIMUM_RECOVERY_SAMPLES:
        raise ValueError(
            "a gradual-loss run needs at least "
            f"{MINIMUM_RECOVERY_SAMPLES} observed recovery samples after clear"
        )

    zero = InjectedCondition(loss_percent=0, delay_ms=0, jitter_ms=0)
    previous_condition = zero
    for planned, _acknowledged in applied_transitions:
        condition = planned.condition
        previous_values = (
            previous_condition.loss_percent,
            previous_condition.delay_ms,
            previous_condition.jitter_ms,
        )
        values = (condition.loss_percent, condition.delay_ms, condition.jitter_ms)
        if any(current < previous for current, previous in zip(values, previous_values)):
            raise ValueError("gradual-loss fault conditions must be component-wise non-decreasing")
        if not any(current > previous for current, previous in zip(values, previous_values)):
            raise ValueError("every gradual-loss fault step must increase at least one condition")
        if observations[planned.sequence].phase != "ramp":
            raise ValueError("every applied fault transition must begin a ramp observation")
        previous_condition = condition
    last_fault_sequence = applied_transitions[-1][0].sequence
    if not any(
        observation.phase == "steady"
        for observation in observations[last_fault_sequence:clear_sequence]
    ):
        raise ValueError("a gradual-loss run must observe the final condition in a steady phase")

    active_condition = zero
    effective_by_sequence = {
        planned.sequence: acknowledged.condition for planned, acknowledged in transitions
    }
    for observation in observations:
        if observation.sequence in effective_by_sequence:
            active_condition = effective_by_sequence[observation.sequence]
        if observation.injected != active_condition:
            raise ValueError(
                "observation injected condition does not match the acknowledged controller state"
            )
        if observation.sequence < first_fault_sequence:
            expected_phases = {"baseline"}
        elif observation.sequence >= clear_sequence:
            expected_phases = {"recovery"}
        else:
            expected_phases = {"ramp", "steady"}
        if observation.phase not in expected_phases:
            raise ValueError("observation phase conflicts with the proven controller state")


def _validate_applied_qdisc(record: PredictiveFaultRecord) -> None:
    snapshot = record.qdisc_snapshot
    if snapshot is None:
        raise ValueError("applied fault record is missing its qdisc snapshot")
    netem = [entry for entry in snapshot if entry.get("kind") == "netem"]
    if len(netem) != 1 or netem[0].get("root") is not True:
        raise ValueError("applied qdisc snapshot must contain one root netem qdisc")
    options = netem[0].get("options")
    if not isinstance(options, dict):
        raise ValueError("applied netem snapshot is missing options")
    expected = record.condition

    # tc omits disabled netem options. Omission proves zero only when zero was
    # actually planned; null, malformed and missing nonzero evidence still fail.
    def option(group: str, key: str, name: str, wanted: float, multiplier: int) -> float:
        if group not in options:
            if wanted == 0:
                return 0.0
            raise ValueError(f"applied netem snapshot is missing {name} evidence")
        values = options[group]
        if not isinstance(values, dict):
            raise ValueError(f"applied netem {group} must be an object")
        if key not in values:
            if wanted == 0:
                return 0.0
            raise ValueError(f"applied netem snapshot is missing {name} evidence")
        return _finite_number(values[key], f"netem {name}") * multiplier

    actual_delay_ms = option("delay", "delay", "delay", expected.delay_ms, 1_000)
    actual_jitter_ms = option("delay", "jitter", "jitter", expected.jitter_ms, 1_000)
    actual_loss_percent = option("loss-random", "loss", "loss", expected.loss_percent, 100)
    for name, actual, wanted in (
        ("loss", actual_loss_percent, expected.loss_percent),
        ("delay", actual_delay_ms, expected.delay_ms),
        ("jitter", actual_jitter_ms, expected.jitter_ms),
    ):
        if abs(actual - wanted) > 0.001:
            raise ValueError(f"applied qdisc {name} does not match the planned condition")


def _validate_cleared_qdisc(record: PredictiveFaultRecord) -> None:
    snapshot = record.qdisc_snapshot
    if snapshot is None:
        raise ValueError("cleared fault record is missing its qdisc snapshot")
    if any(entry.get("kind") == "netem" for entry in snapshot):
        raise ValueError("cleared qdisc snapshot still contains netem")


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    converted = float(value)
    if not (-float("inf") < converted < float("inf")):
        raise ValueError(f"{name} must be finite")
    return converted
