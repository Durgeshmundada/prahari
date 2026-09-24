"""Verified loading and live inference for the predictive-v2 horizon bundle."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import lightgbm as lgb
import numpy as np

from prahari.analytics.predictive import (
    FEATURE_NAMES,
    FEATURE_VERSION,
    MAX_MEASUREMENT_JOIN_SKEW_SECONDS,
    ApplicationImpactPolicy,
    ApplicationMeasurement,
    NetworkMeasurement,
    PredictiveModelApproval,
    PredictivePoint,
    calibrate_probability,
    extract_predictive_feature_vectors,
    monotonic_horizon_probabilities,
    parse_strict_json,
)
from prahari.contracts import AppProbeRecord, MetricRecord

HORIZONS = (30, 60, 120)
TECHNICAL_EVALUATION_POLICY_VERSION = "technical-evaluation-v2"
FALSE_ALERT_RATE_CONFIDENCE = 0.95
MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR = 1.0
_REQUIRED_GATE_CHECKS = frozenset(
    {
        "validation_incident_precision_at_least_0_70",
        "validation_incident_recall_at_least_0_60",
        "validation_false_alerts_per_negative_link_hour_at_most_1_0",
        "validation_false_alert_rate_upper_bound_at_most_1_0",
        "validation_median_useful_lead_at_least_30_seconds",
        "test_incident_precision_at_least_0_70",
        "test_incident_recall_at_least_0_60",
        "test_brier_at_most_0_25",
        "test_false_alerts_per_negative_link_hour_at_most_1_0",
        "test_false_alert_rate_upper_bound_at_most_1_0",
        "test_median_useful_lead_at_least_30_seconds",
    }
)
_SAFE_IDENTIFIER_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_REQUIRED_MANIFEST_FIELDS = {
    "schema_version",
    "model_version",
    "model_kind",
    "approval_status",
    "predictive_claim_approved",
    "deployment_scope",
    "feature_version",
    "features",
    "horizons_seconds",
    "sampling_seconds",
    "window_seconds",
    "minimum_samples",
    "dataset_version",
    "dataset_sha256",
    "dataset_manifest_sha256",
    "impact_policy",
    "run_counts",
    "row_counts",
    "models",
    "evaluation",
    "technical_evaluation_gate",
    "limitations",
}


@dataclass(frozen=True)
class HorizonRiskScore:
    probabilities: dict[int, float]
    thresholds: dict[int, float]
    alerts: dict[int, bool]
    model_version: str
    feature_version: str
    approval_status: str
    deployment_scope: str
    window_end: datetime


@dataclass(frozen=True)
class _LoadedHorizon:
    booster: lgb.Booster
    calibration_slope: float
    calibration_intercept: float
    threshold: float


def technical_evaluation_gate_passed(value: object) -> bool:
    """Require the current uncertainty-aware technical gate for activation."""

    if not isinstance(value, dict) or value.get("passed") is not True:
        return False
    if value.get("policy_version") != TECHNICAL_EVALUATION_POLICY_VERSION:
        return False
    policy = value.get("false_alert_rate_policy")
    if not isinstance(policy, dict):
        return False
    confidence = policy.get("one_sided_confidence")
    maximum_rate = policy.get("maximum_per_negative_link_hour")
    if (
        set(policy)
        != {"maximum_per_negative_link_hour", "one_sided_confidence", "method"}
        or policy.get("method") != "exact Poisson upper confidence bound"
        or isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or confidence != FALSE_ALERT_RATE_CONFIDENCE
        or isinstance(maximum_rate, bool)
        or not isinstance(maximum_rate, (int, float))
        or maximum_rate != MAX_FALSE_ALERTS_PER_NEGATIVE_LINK_HOUR
        or value.get("selection_data") != ["validation"]
        or value.get("evaluation_data") != ["test"]
        or value.get("excluded_from_gate") != ["demo_holdout"]
        or value.get("does_not_grant_approval") is not True
    ):
        return False
    checks = value.get("checks")
    return isinstance(checks, dict) and all(
        checks.get(name) is True for name in _REQUIRED_GATE_CHECKS
    )


class PredictiveHorizonClassifier:
    """Fail-closed model bundle whose three outputs share one runtime feature contract."""

    def __init__(self, manifest_path: Path) -> None:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("predictive model manifest must be a non-symlink file")
        model_directory = manifest_path.parent
        if model_directory.is_symlink():
            raise ValueError("predictive model directory must not be a symlink")
        manifest_bytes = manifest_path.read_bytes()
        self.bundle_sha256 = sha256(manifest_bytes).hexdigest()
        if not manifest_bytes.endswith(b"\n"):
            raise ValueError("predictive model manifest must end with a newline")
        manifest = parse_strict_json(manifest_bytes)
        if not isinstance(manifest, dict):
            raise ValueError("predictive model manifest must be an object")
        missing = _REQUIRED_MANIFEST_FIELDS - set(manifest)
        unknown = set(manifest) - _REQUIRED_MANIFEST_FIELDS - {"approval_record"}
        if missing or unknown:
            raise ValueError(
                "predictive model manifest fields are incomplete or unexpected: "
                f"missing={sorted(missing)}, unknown={sorted(unknown)}"
            )
        if manifest.get("schema_version") != "predictive-model-manifest-v2":
            raise ValueError("unsupported predictive model manifest schema")
        if manifest.get("feature_version") != FEATURE_VERSION:
            raise ValueError("predictive feature version does not match the runtime")
        if tuple(manifest.get("features", [])) != FEATURE_NAMES:
            raise ValueError("predictive model feature contract does not match the runtime")
        if tuple(manifest.get("horizons_seconds", [])) != HORIZONS:
            raise ValueError("predictive model horizons do not match the runtime")
        if not _is_sha256(manifest.get("dataset_sha256")) or not _is_sha256(
            manifest.get("dataset_manifest_sha256")
        ):
            raise ValueError("predictive model dataset bindings are invalid")
        self.model_version = str(manifest.get("model_version", ""))
        if (
            not 3 <= len(self.model_version) <= 100
            or any(
                character not in _SAFE_IDENTIFIER_CHARACTERS
                for character in self.model_version
            )
        ):
            raise ValueError("predictive model version must be a safe identifier")
        self.approval_status = str(manifest.get("approval_status", ""))
        self.predictive_claim_approved = manifest.get("predictive_claim_approved") is True
        self.deployment_scope = str(manifest.get("deployment_scope", ""))
        if self.predictive_claim_approved:
            valid_approval_state = (
                self.approval_status == "approved"
                and self.deployment_scope in {"controlled_lab", "production"}
                and isinstance(manifest.get("approval_record"), dict)
                and technical_evaluation_gate_passed(manifest.get("technical_evaluation_gate"))
            )
        else:
            valid_approval_state = (
                self.approval_status == "evaluation_pending"
                and self.deployment_scope == "none"
                and "approval_record" not in manifest
            )
        if not valid_approval_state:
            raise ValueError("predictive approval fields are internally inconsistent")
        self.activated = (
            self.predictive_claim_approved
            and self.approval_status == "approved"
            and self.deployment_scope in {"controlled_lab", "production"}
        )
        self.network_profile_sha256: str | None = None
        if self.activated:
            approval = _verify_approval_record(model_directory, manifest)
            self.network_profile_sha256 = approval.network_profile_sha256
        self.impact_policy = ApplicationImpactPolicy.model_validate(
            manifest.get("impact_policy")
        )
        self.sampling_seconds = _bounded_integer(
            manifest.get("sampling_seconds"), "sampling_seconds", 1, 30
        )
        self.window_seconds = _bounded_integer(
            manifest.get("window_seconds"), "window_seconds", 30, 300
        )
        self.minimum_samples = _bounded_integer(
            manifest.get("minimum_samples"), "minimum_samples", 2, 100
        )
        models = manifest.get("models")
        if not isinstance(models, dict) or set(models) != {str(value) for value in HORIZONS}:
            raise ValueError("predictive manifest must contain exactly three horizon models")
        self._models = {
            horizon: _load_horizon(model_directory, models[str(horizon)], horizon)
            for horizon in HORIZONS
        }
        self.thresholds = {
            horizon: self._models[horizon].threshold for horizon in HORIZONS
        }

    def score(
        self,
        points: list[PredictivePoint],
        *,
        require_approved: bool = True,
        reference_time: datetime | None = None,
    ) -> HorizonRiskScore:
        if require_approved and not self.activated:
            raise ValueError("predictive model is not approved for runtime risk")
        vectors = extract_predictive_feature_vectors(
            points,
            run_id=None,
            window_seconds=self.window_seconds,
            sampling_seconds=self.sampling_seconds,
            minimum_samples=self.minimum_samples,
            max_inter_sample_gap_seconds=self.impact_policy.max_inter_sample_gap_seconds,
        )
        if not vectors:
            raise ValueError("no complete predictive feature window is available")
        vector = vectors[-1]
        if require_approved or reference_time is not None:
            effective_reference = reference_time or datetime.now(UTC)
            if effective_reference.tzinfo is None or effective_reference.utcoffset() is None:
                raise ValueError("predictive score reference time must include a UTC offset")
            age_seconds = (
                effective_reference.astimezone(UTC) - vector.window_end
            ).total_seconds()
            if not 0 <= age_seconds <= self.impact_policy.max_inter_sample_gap_seconds:
                raise ValueError("predictive feature window is stale or future-dated")
        values = np.asarray([[vector.values[name] for name in FEATURE_NAMES]], dtype=float)
        raw = []
        for horizon in HORIZONS:
            loaded = self._models[horizon]
            probability = float(loaded.booster.predict(values)[0])
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ValueError("predictive model returned an invalid probability")
            raw.append(
                calibrate_probability(
                    probability,
                    loaded.calibration_slope,
                    loaded.calibration_intercept,
                )
            )
        monotonic = monotonic_horizon_probabilities(raw)
        probabilities = dict(zip(HORIZONS, monotonic, strict=True))
        thresholds = dict(self.thresholds)
        return HorizonRiskScore(
            probabilities=probabilities,
            thresholds=thresholds,
            alerts={
                horizon: probabilities[horizon] >= thresholds[horizon]
                for horizon in HORIZONS
            },
            model_version=self.model_version,
            feature_version=FEATURE_VERSION,
            approval_status=self.approval_status,
            deployment_scope=self.deployment_scope,
            window_end=vector.window_end,
        )


def join_runtime_points(
    metrics: list[MetricRecord],
    probes: list[AppProbeRecord],
    *,
    maximum_skew_seconds: float = MAX_MEASUREMENT_JOIN_SKEW_SECONDS,
    entity_id: str = "tunnel:branch-01-to-dc-primary",
    path_id: str = "path:branch-01-to-dc:primary",
    topology_version: str = "lab-v1",
    config_version: str = "lab-config-v1",
    app_probe_entities: dict[str, str] | None = None,
) -> list[PredictivePoint]:
    """Join live network and same-path app records without inventing missing values."""

    if maximum_skew_seconds <= 0:
        raise ValueError("maximum runtime join skew must be positive")
    expected_apps = app_probe_entities if app_probe_entities is not None else {
        "chat": "app:chat:branch-01-to-dc",
        "file-upload": "app:file-upload:branch-01-to-dc",
    }
    if not {"chat", "file-upload"} <= set(expected_apps):
        raise ValueError("predictive horizons require configured chat and file-upload probes")
    network_groups: dict[datetime, dict[str, MetricRecord]] = {}
    for record in metrics:
        if (
            record.metric not in {"packet_loss_ratio", "jitter_ms"}
            or record.entity_id != entity_id
            or record.labels.get("path_id") != path_id
            or record.topology_version != topology_version
            or record.config_version != config_version
            or not _usable_quality(record, maximum_skew_seconds)
            or (record.metric == "packet_loss_ratio" and record.unit != "ratio")
            or (record.metric == "jitter_ms" and record.unit != "ms")
        ):
            continue
        group = network_groups.setdefault(record.observed_at, {})
        if record.metric in group:
            raise ValueError("runtime network evidence contains a duplicate metric timestamp")
        group[record.metric] = record
    app_groups: dict[datetime, dict[str, AppProbeRecord]] = {}
    for probe in probes:
        if (
            probe.path_id == path_id
            and probe.probe_name in {"chat", "file-upload"}
            and probe.entity_id
            == expected_apps[probe.probe_name]
            and probe.topology_version == topology_version
            and probe.config_version == config_version
            and _usable_quality(probe, maximum_skew_seconds)
        ):
            group = app_groups.setdefault(probe.observed_at, {})
            if probe.probe_name in group:
                raise ValueError(
                    "runtime application evidence contains a duplicate probe timestamp"
                )
            group[probe.probe_name] = probe
    complete_apps = {
        observed_at: group
        for observed_at, group in app_groups.items()
        if set(group) == {"chat", "file-upload"}
    }
    points: list[PredictivePoint] = []
    used_network_times: set[datetime] = set()
    for app_at, app_group in sorted(complete_apps.items()):
        candidates = [
            network_at
            for network_at, group in network_groups.items()
            if network_at not in used_network_times
            and set(group) == {"packet_loss_ratio", "jitter_ms"}
            and abs((app_at - network_at).total_seconds()) <= maximum_skew_seconds
        ]
        if not candidates:
            continue
        network_at = min(
            candidates,
            key=lambda candidate: (abs((app_at - candidate).total_seconds()), candidate),
        )
        used_network_times.add(network_at)
        group = network_groups[network_at]
        if set(group) != {"packet_loss_ratio", "jitter_ms"}:
            continue
        loss = group["packet_loss_ratio"]
        jitter = group["jitter_ms"]
        try:
            rtt = float(loss.labels["rtt_avg_ms"])
        except (KeyError, ValueError) as error:
            raise ValueError("runtime network sample is missing valid RTT evidence") from error
        if not math.isfinite(rtt) or rtt < 0:
            raise ValueError("runtime network sample has invalid RTT evidence")
        chat = app_group["chat"]
        upload_probe = app_group["file-upload"]
        transfer = upload_probe.transfer
        if transfer is None or transfer.chat_measurements is None:
            raise ValueError("runtime file probe is missing joined chat/transfer evidence")
        measured_chat = transfer.chat_measurements
        if (
            abs(measured_chat.computed_message_loss_ratio - chat.packet_loss_ratio) > 1e-6
            or abs(measured_chat.rtt_p95_ms - chat.request_latency_ms) > 1e-6
            or abs(measured_chat.jitter_ms - chat.jitter_ms) > 1e-6
        ):
            raise ValueError("runtime chat and transfer evidence disagree")
        upload_status = "completed" if transfer.status == "completed" else "failed"
        upload_integrity = "verified" if transfer.integrity == "verified" else "failed"
        app_status = (
            "passed"
            if measured_chat.computed_message_loss_ratio == 0
            and upload_status == "completed"
            and upload_integrity == "verified"
            else "failed"
        )
        points.append(
            PredictivePoint(
                # Timestamp the joined point at the later component so a live
                # score never treats a future network observation as already
                # known at the application-probe time.
                observed_at=max(app_at, network_at),
                network=NetworkMeasurement(
                    packet_loss_ratio=loss.value,
                    jitter_ms=jitter.value,
                    rtt_avg_ms=rtt,
                ),
                application=ApplicationMeasurement(
                    status=app_status,
                    messages_attempted=measured_chat.messages_attempted,
                    messages_received=measured_chat.messages_received,
                    message_loss_ratio=measured_chat.computed_message_loss_ratio,
                    rtt_p95_ms=measured_chat.rtt_p95_ms,
                    jitter_ms=measured_chat.jitter_ms,
                    upload_status=upload_status,
                    upload_integrity=upload_integrity,
                    upload_throughput_mbps=transfer.throughput_mbps or 0,
                    evidence_saved=True,
                ),
            )
        )
    return points


def _usable_quality(record: MetricRecord | AppProbeRecord, maximum_skew_seconds: float) -> bool:
    quality = record.quality
    return (
        quality.source_healthy
        and not quality.is_estimated
        and not quality.counter_reset
        and quality.late_by_ms <= maximum_skew_seconds * 1_000
    )


def _load_horizon(directory: Path, raw: object, horizon: int) -> _LoadedHorizon:
    if not isinstance(raw, dict):
        raise ValueError(f"horizon {horizon} model metadata must be an object")
    expected_fields = {
        "file",
        "sha256",
        "best_iteration",
        "calibration",
        "decision_threshold",
        "top_features_by_gain",
    }
    if set(raw) != expected_fields:
        raise ValueError(f"horizon {horizon} model metadata fields are invalid")
    file_name = raw.get("file")
    if not isinstance(file_name, str) or Path(file_name).name != file_name:
        raise ValueError(f"horizon {horizon} model file must be a basename")
    model_path = directory / file_name
    if model_path.is_symlink() or not model_path.is_file():
        raise ValueError(f"horizon {horizon} model must be a non-symlink file")
    expected_hash = raw.get("sha256")
    actual_hash = sha256(model_path.read_bytes()).hexdigest()
    if not isinstance(expected_hash, str) or actual_hash != expected_hash:
        raise ValueError(f"horizon {horizon} model checksum does not match")
    calibration = raw.get("calibration")
    if (
        not isinstance(calibration, dict)
        or set(calibration) != {"method", "slope", "intercept"}
        or calibration.get("method") != "platt_logistic"
    ):
        raise ValueError(f"horizon {horizon} calibration metadata is invalid")
    slope = _finite_float(calibration.get("slope"), f"horizon {horizon} calibration slope")
    if slope <= 0:
        raise ValueError(f"horizon {horizon} calibration slope must be positive")
    intercept = _finite_float(
        calibration.get("intercept"), f"horizon {horizon} calibration intercept"
    )
    threshold = _finite_float(raw.get("decision_threshold"), f"horizon {horizon} threshold")
    if not 0 < threshold < 1:
        raise ValueError(f"horizon {horizon} threshold must be between zero and one")
    booster = lgb.Booster(model_file=str(model_path))
    if tuple(booster.feature_name()) != FEATURE_NAMES:
        raise ValueError(f"horizon {horizon} model feature names do not match runtime")
    return _LoadedHorizon(booster, slope, intercept, threshold)


def _verify_approval_record(
    directory: Path,
    active_manifest: dict[str, object],
) -> PredictiveModelApproval:
    raw = active_manifest.get("approval_record")
    if not isinstance(raw, dict):
        raise ValueError("approved predictive model is missing approval metadata")
    if set(raw) != {
        "file",
        "sha256",
        "candidate_manifest_file",
        "candidate_manifest_sha256",
        "reviewer_id",
        "reviewed_at",
    }:
        raise ValueError("approved predictive model has unexpected approval metadata")
    file_name = raw.get("file")
    if not isinstance(file_name, str) or Path(file_name).name != file_name:
        raise ValueError("approval record file must be a basename")
    approval_path = directory / file_name
    if approval_path.is_symlink() or not approval_path.is_file():
        raise ValueError("approval record must be a non-symlink file")
    approval_bytes = approval_path.read_bytes()
    if not approval_bytes.endswith(b"\n"):
        raise ValueError("approval record must end with a newline")
    if sha256(approval_bytes).hexdigest() != raw.get("sha256"):
        raise ValueError("approval record checksum does not match")
    approval = PredictiveModelApproval.model_validate(parse_strict_json(approval_bytes))
    candidate_name = raw.get("candidate_manifest_file")
    if not isinstance(candidate_name, str) or Path(candidate_name).name != candidate_name:
        raise ValueError("candidate manifest file must be a basename")
    candidate_path = directory / candidate_name
    if candidate_path.is_symlink() or not candidate_path.is_file():
        raise ValueError("candidate manifest must be a non-symlink file")
    candidate_bytes = candidate_path.read_bytes()
    if not candidate_bytes.endswith(b"\n"):
        raise ValueError("candidate manifest must end with a newline")
    candidate_hash = sha256(candidate_bytes).hexdigest()
    if candidate_hash != raw.get("candidate_manifest_sha256"):
        raise ValueError("candidate manifest checksum does not match registration")
    if approval.candidate_manifest_sha256 != candidate_hash:
        raise ValueError("approval record does not bind the candidate manifest")
    candidate_manifest = parse_strict_json(candidate_bytes)
    expected_candidate = dict(active_manifest)
    expected_candidate.pop("approval_record", None)
    expected_candidate.update(
        {
            "approval_status": "evaluation_pending",
            "predictive_claim_approved": False,
            "deployment_scope": "none",
        }
    )
    if candidate_manifest != expected_candidate:
        raise ValueError("approved manifest differs from the reviewed candidate")
    if (
        approval.model_version != active_manifest.get("model_version")
        or approval.dataset_sha256 != active_manifest.get("dataset_sha256")
        or approval.dataset_manifest_sha256
        != active_manifest.get("dataset_manifest_sha256")
        or approval.deployment_scope != active_manifest.get("deployment_scope")
        or approval.reviewer_id != raw.get("reviewer_id")
        or approval.reviewed_at.isoformat() != raw.get("reviewed_at")
    ):
        raise ValueError("approval record does not match the registered model")
    return approval


def _finite_float(raw: object, name: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} must be numeric")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _is_sha256(raw: object) -> bool:
    return (
        isinstance(raw, str)
        and len(raw) == 64
        and all(character in "0123456789abcdef" for character in raw)
    )


def _bounded_integer(raw: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or not minimum <= raw <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return raw
