import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest

from prahari.analytics.horizon import (
    PredictiveHorizonClassifier,
    join_runtime_points,
    technical_evaluation_gate_passed,
)
from prahari.analytics.predictive import (
    FEATURE_NAMES,
    ApplicationMeasurement,
    NetworkMeasurement,
    PredictivePoint,
)
from prahari.contracts import AppProbeRecord, MetricRecord, RecordQuality
from prahari.network_profile import load_network_profile
from prahari.services.demo import PrahariService
from prahari.traffic_demo.protocol import ProbeMeasurements, UploadMeasurement
from prahari.traffic_demo.telemetry import TrafficTelemetryContext, build_app_probe_records
from scripts.approve_predictive_model import approve


def _technical_gate(passed: bool) -> dict[str, object]:
    checks = {
        name: passed
        for name in (
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
        )
    }
    return {
        "policy_version": "technical-evaluation-v2",
        "passed": passed,
        "checks": checks,
        "false_alert_rate_policy": {
            "maximum_per_negative_link_hour": 1.0,
            "one_sided_confidence": 0.95,
            "method": "exact Poisson upper confidence bound",
        },
        "selection_data": ["validation"],
        "evaluation_data": ["test"],
        "excluded_from_gate": ["demo_holdout"],
        "does_not_grant_approval": True,
    }


def _model_bundle(
    directory: Path,
    *,
    approved: bool = False,
    network_profile_sha256: str | None = None,
) -> Path:
    models = {}
    x_values = np.asarray(
        [[float((row + column) % 7) for column in range(len(FEATURE_NAMES))] for row in range(20)]
    )
    labels = np.asarray([row % 2 for row in range(20)])
    for horizon in (30, 60, 120):
        booster = lgb.train(
            {
                "objective": "binary",
                "verbosity": -1,
                "num_leaves": 2,
                "min_data_in_leaf": 1,
                "num_threads": 1,
            },
            lgb.Dataset(x_values, label=labels, feature_name=list(FEATURE_NAMES)),
            num_boost_round=2,
        )
        model_path = directory / f"risk-within-{horizon}s.txt"
        booster.save_model(model_path)
        models[str(horizon)] = {
            "file": model_path.name,
            "sha256": sha256(model_path.read_bytes()).hexdigest(),
            "best_iteration": 2,
            "calibration": {"method": "platt_logistic", "slope": 1, "intercept": 0},
            "decision_threshold": 0.5,
            "top_features_by_gain": [],
        }
    manifest = {
        "schema_version": "predictive-model-manifest-v2",
        "model_version": "risk-test-v2",
        "model_kind": "test horizon bundle",
        "approval_status": "evaluation_pending",
        "predictive_claim_approved": False,
        "deployment_scope": "none",
        "feature_version": "predictive-window-v2",
        "features": list(FEATURE_NAMES),
        "horizons_seconds": [30, 60, 120],
        "sampling_seconds": 5,
        "window_seconds": 60,
        "minimum_samples": 10,
        "dataset_sha256": "1" * 64,
        "dataset_manifest_sha256": "2" * 64,
        "dataset_version": "test-dataset-v2",
        "impact_policy": {
            "schema_version": "application-impact-policy-v2",
            "version": "test-v2",
            "sustained_seconds": 15,
            "max_inter_sample_gap_seconds": 15,
            "message_loss_ratio": 0.2,
            "rtt_p95_ms": 150,
            "jitter_ms": 30,
            "failed_upload_is_impact": True,
        },
        "models": models,
        "run_counts": {},
        "row_counts": {},
        "evaluation": {},
        "technical_evaluation_gate": _technical_gate(approved),
        "limitations": ["unit test only"],
    }
    if approved:
        candidate_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
        candidate_hash = sha256(candidate_bytes).hexdigest()
        (directory / "candidate-manifest.json").write_bytes(candidate_bytes)
        approval = {
            "schema_version": "predictive-model-approval-v2",
            "decision": "approved",
            "model_version": "risk-test-v2",
            "dataset_sha256": "1" * 64,
            "dataset_manifest_sha256": "2" * 64,
            "candidate_manifest_sha256": candidate_hash,
            "deployment_scope": "controlled_lab",
            "reviewer_id": "reviewer-test",
            "reviewed_at": "2026-09-03T00:00:00+00:00",
            "rationale": "The independent test evidence and limitations were reviewed.",
            "technical_gate_reviewed": True,
            "untouched_holdout_reviewed": True,
            "limitations_accepted": True,
            "external_validation_evidence_sha256": None,
            "change_ticket": None,
            "network_profile_sha256": network_profile_sha256,
        }
        approval_bytes = (json.dumps(approval, sort_keys=True) + "\n").encode()
        (directory / "approval-record.json").write_bytes(approval_bytes)
        manifest.update(
            {
                "approval_status": "approved",
                "predictive_claim_approved": True,
                "deployment_scope": "controlled_lab",
                "approval_record": {
                    "file": "approval-record.json",
                    "sha256": sha256(approval_bytes).hexdigest(),
                    "candidate_manifest_file": "candidate-manifest.json",
                    "candidate_manifest_sha256": candidate_hash,
                    "reviewer_id": "reviewer-test",
                    "reviewed_at": "2026-09-03T00:00:00+00:00",
                },
            }
        )
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def test_activation_requires_the_uncertainty_aware_technical_gate() -> None:
    assert technical_evaluation_gate_passed({"passed": True}) is False
    assert technical_evaluation_gate_passed(_technical_gate(True)) is True
    changed = _technical_gate(True)
    changed["false_alert_rate_policy"]["one_sided_confidence"] = 0.90
    assert technical_evaluation_gate_passed(changed) is False


def test_approval_rejects_a_legacy_passed_flag(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "predictive-model-manifest-v2",
                "approval_status": "evaluation_pending",
                "technical_evaluation_gate": {"passed": True},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="uncertainty-aware"):
        approve(manifest, tmp_path / "unused-approval.json")


def _points() -> list[PredictivePoint]:
    return [
        PredictivePoint(
            observed_at=datetime(2026, 9, 1, tzinfo=UTC) + timedelta(seconds=index * 5),
            network=NetworkMeasurement(
                packet_loss_ratio=index / 100,
                rtt_avg_ms=1 + index,
                jitter_ms=index / 10,
            ),
            application=ApplicationMeasurement(
                status="passed",
                messages_attempted=5,
                messages_received=5,
                message_loss_ratio=0,
                rtt_p95_ms=2 + index,
                jitter_ms=1,
                upload_status="completed",
                upload_integrity="verified",
                upload_throughput_mbps=10,
                evidence_saved=True,
            ),
        )
        for index in range(14)
    ]


def test_unapproved_horizon_bundle_can_be_evaluated_but_not_activated(tmp_path: Path) -> None:
    classifier = PredictiveHorizonClassifier(_model_bundle(tmp_path))

    with pytest.raises(ValueError, match="not approved"):
        classifier.score(_points())

    score = classifier.score(_points(), require_approved=False)
    assert list(score.probabilities) == [30, 60, 120]
    assert score.probabilities[30] <= score.probabilities[60] <= score.probabilities[120]
    assert score.approval_status == "evaluation_pending"


def test_horizon_bundle_rejects_a_changed_model(tmp_path: Path) -> None:
    manifest_path = _model_bundle(tmp_path)
    (tmp_path / "risk-within-60s.txt").write_text("changed", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum"):
        PredictiveHorizonClassifier(manifest_path)


def test_approved_horizon_bundle_activates_only_with_bound_record(tmp_path: Path) -> None:
    classifier = PredictiveHorizonClassifier(_model_bundle(tmp_path, approved=True))
    points = _points()

    score = classifier.score(points, reference_time=points[-1].observed_at)

    assert classifier.activated is True
    assert score.deployment_scope == "controlled_lab"


@pytest.mark.parametrize("external", [False, True])
def test_runtime_join_uses_matching_network_chat_and_file_evidence(external: bool) -> None:
    metrics: list[MetricRecord] = []
    probes: list[AppProbeRecord] = []
    base = datetime(2026, 9, 1, tzinfo=UTC)
    for index in range(14):
        observed_at = base + timedelta(seconds=index * 5)
        common = {
            "observed_at": observed_at,
            "source": "network-probe-branch",
            "entity_id": "tunnel:branch-01-to-dc-primary",
            "entity_type": "path",
            "labels": {
                "path_id": "path:branch-01-to-dc:primary",
                "rtt_avg_ms": str(2 + index),
            },
            "topology_version": "lab-v1",
            "config_version": "lab-config-v1",
            "quality": RecordQuality(source_healthy=True),
        }
        metrics.extend(
            [
                MetricRecord(metric="packet_loss_ratio", value=0, unit="ratio", **common),
                MetricRecord(metric="jitter_ms", value=1, unit="ms", **common),
            ]
        )
        chat = ProbeMeasurements(attempted=5, received=5, rtts_ms=(2, 3, 4, 5, 6))
        upload = UploadMeasurement(
            transfer_id=f"transfer-{index}",
            file_name="test.bin",
            bytes_expected=1024,
            bytes_transferred=1024,
            bytes_received=1024,
            elapsed_seconds=0.01,
            checksum_sha256="1" * 64,
            integrity="verified",
            status="completed",
        )
        probes.extend(
            AppProbeRecord.model_validate(raw)
            for raw in build_app_probe_records(
                context=TrafficTelemetryContext(run_id="runtime-join-test"),
                observed_at=observed_at,
                chat=chat,
                upload=upload,
                kind="file",
            )
        )

    if external:
        metrics = [
            record.model_copy(
                update={
                    "entity_id": "target:tunnel",
                    "topology_version": "target-v2",
                    "config_version": "target-config-v2",
                    "labels": {**record.labels, "path_id": "target:path"},
                }
            )
            for record in metrics
        ]
        probes = [
            record.model_copy(
                update={
                    "entity_id": f"target:{record.probe_name}",
                    "path_id": "target:path",
                    "topology_version": "target-v2",
                    "config_version": "target-config-v2",
                }
            )
            for record in probes
        ]
        assert join_runtime_points(metrics, probes) == []
        points = join_runtime_points(
            metrics,
            probes,
            entity_id="target:tunnel",
            path_id="target:path",
            topology_version="target-v2",
            config_version="target-config-v2",
            app_probe_entities={"chat": "target:chat", "file-upload": "target:file-upload"},
        )
    else:
        points = join_runtime_points(metrics, probes)

    assert len(points) == 14
    assert points[-1].network.rtt_avg_ms == 15
    assert points[-1].application.rtt_p95_ms == 6


def test_service_rejects_an_approval_bound_to_another_profile(
    project_root: Path,
    tmp_path: Path,
) -> None:
    profile = load_network_profile(project_root / "config/network-profile.lab.example.json")
    manifest = _model_bundle(tmp_path, approved=True, network_profile_sha256=profile.fingerprint)
    service = PrahariService(
        root=project_root,
        data_mode="live",
        network_profile=profile,
        predictive_model_manifest_path=manifest,
    )
    assert service.model_info()["training_status"] == "registered and evaluated"
    changed = profile.model_copy(update={"profile_id": "different-profile"})
    with pytest.raises(ValueError, match="does not bind"):
        PrahariService(
            root=project_root,
            data_mode="live",
            network_profile=changed,
            predictive_model_manifest_path=manifest,
        )
