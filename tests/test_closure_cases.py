from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from prahari.analytics.baseline import PredictionWithheldError
from prahari.api import create_app
from prahari.contracts import (
    AppProbeRecord,
    ClosureRequest,
    ClosureReviewRequest,
    RecordQuality,
    recovery_evidence_sha256,
)
from prahari.persistence import PersistenceStore
from prahari.services.demo import DemoService
from prahari.settings import Settings


def test_closure_snapshot_is_not_saved_when_another_worker_supersedes_review(
    project_root: Path,
    monkeypatch,
) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    original_close = store.close_with_audit

    def supersede_review(incident_id, request, prediction_id, **kwargs):
        store.review_with_audit(
            incident_id, "operator-02", "Independent worker superseded review.", prediction_id
        )
        return original_close(incident_id, request, prediction_id, **kwargs)

    monkeypatch.setattr(store, "close_with_audit", supersede_review)
    with pytest.raises(ValueError, match="current operator review"):
        submit_case(service)
    assert store.list_closure_cases() == []
    assert store.incident_state(service.incident_id).reviewed_by == "operator-02"
    store.dispose()


def submit_case(service: DemoService, note: str = "Application recovery checked."):
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(actor_id="operator-01", outcome="resolved", note=note),
    )
    return service.list_closure_cases()[0]


def _recovery_probe(
    observed_at: datetime, *, impacted: bool, probe_name: str = "video"
) -> AppProbeRecord:
    entities = {
        "video": "app:video:branch-01-to-dc",
        "chat": "app:chat:branch-01-to-dc",
    }
    return AppProbeRecord(
        observed_at=observed_at,
        entity_id=entities[probe_name],
        probe_name=probe_name,
        packet_loss_ratio=0.08 if impacted else 0.01,
        jitter_ms=40 if impacted else 8,
        request_latency_ms=250 if impacted else 80,
        path_id="path:branch-01-to-dc:primary",
        topology_version="lab-v1",
        config_version="lab-config-v1",
        quality=RecordQuality(source_healthy=True),
    )


def test_closure_captures_exact_sustained_before_after_recovery(project_root: Path) -> None:
    service = DemoService(root=project_root)
    intervention = datetime(2026, 8, 20, 9, 1, 20, tzinfo=UTC)
    service._app_probes = [
        *[
            _recovery_probe(intervention - timedelta(seconds=offset), impacted=True)
            for offset in (20, 15, 10, 5)
        ],
        *[
            _recovery_probe(
                intervention - timedelta(seconds=offset),
                impacted=False,
                probe_name="chat",
            )
            for offset in (20, 15, 10, 5)
        ],
        _recovery_probe(intervention, impacted=True),
        *[
            _recovery_probe(intervention + timedelta(seconds=offset), impacted=False)
            for offset in (5, 10, 15, 20)
        ],
        *[
            _recovery_probe(
                intervention + timedelta(seconds=offset),
                impacted=False,
                probe_name="chat",
            )
            for offset in (5, 10, 15, 20)
        ],
    ]
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(
            actor_id="operator-01",
            outcome="resolved",
            note="Measured recovery after intervention.",
            intervention_at=intervention,
        ),
    )

    comparison = service.list_closure_cases()[0].recovery_comparison
    assert comparison is not None
    assert comparison.recovered is True
    assert comparison.before.impact_detected is True
    assert comparison.after.within_policy is True
    assert len(comparison.before.samples) == 4
    assert {sample.probe_name for sample in comparison.before.samples} == {"video"}
    assert len(comparison.after.samples) == 8
    assert {sample.probe_name for sample in comparison.after.samples} == {"video", "chat"}
    assert comparison.schema_version == "recovery-comparison-v2"
    assert comparison.impact_thresholds is not None
    assert comparison.impact_thresholds.packet_loss_ratio == 0.05
    assert comparison.evidence_sha256 != "0" * 64
    tampered = comparison.model_dump(mode="json")
    tampered["after"]["samples"][0]["jitter_ms"] = 9
    with pytest.raises(ValidationError, match="maxima do not match|SHA-256 does not match"):
        type(comparison).model_validate(tampered)
    tampered_policy = comparison.model_dump(mode="json")
    tampered_policy["impact_policy_version"] = "substituted-policy"
    with pytest.raises(ValidationError, match="SHA-256 does not match"):
        type(comparison).model_validate(tampered_policy)
    tampered_threshold = comparison.model_dump(mode="json")
    tampered_threshold["impact_thresholds"]["jitter_ms"] = 45
    with pytest.raises(ValidationError, match="verdict does not match|SHA-256 does not match"):
        type(comparison).model_validate(tampered_threshold)
    legacy = comparison.model_dump(mode="json")
    legacy["schema_version"] = "recovery-comparison-v1"
    legacy["impact_thresholds"] = None
    legacy["evidence_sha256"] = recovery_evidence_sha256(
        schema_version="recovery-comparison-v1",
        path_id=comparison.path_id,
        intervention_at=comparison.intervention_at,
        impact_policy_version=comparison.impact_policy_version,
        impact_thresholds=None,
        required_sustained_seconds=comparison.required_sustained_seconds,
        before=comparison.before,
        after=comparison.after,
        recovered=comparison.recovered,
    )
    legacy_comparison = type(comparison).model_validate(legacy)
    assert legacy_comparison.schema_version == "recovery-comparison-v1"
    case = service.list_closure_cases()[0]
    service._cases[case.id] = case.model_copy(
        update={"recovery_comparison": legacy_comparison}
    )
    with pytest.raises(PredictionWithheldError, match="legacy recovery evidence"):
        service.decide_closure_case(
            case.id,
            ClosureReviewRequest(
                actor_id="reviewer-01",
                decision="approved",
                note="Legacy evidence must not establish recovery.",
            ),
        )


def test_declared_recovery_rejects_non_degraded_before_window(project_root: Path) -> None:
    service = DemoService(root=project_root)
    intervention = datetime(2026, 8, 20, 9, 1, 20, tzinfo=UTC)
    service._app_probes = [
        _recovery_probe(intervention + timedelta(seconds=offset), impacted=False)
        for offset in (-20, -15, -10, -5, 0, 5, 10, 15)
    ]
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    with pytest.raises(PredictionWithheldError, match="pre-intervention impact"):
        service.close(
            service.incident_id,
            ClosureRequest(
                actor_id="operator-01",
                outcome="resolved",
                note="Invalid recovery claim.",
                intervention_at=intervention,
            ),
        )


def test_declared_recovery_requires_post_evidence_for_impacted_stream(
    project_root: Path,
) -> None:
    service = DemoService(root=project_root)
    intervention = datetime(2026, 8, 20, 9, 1, 20, tzinfo=UTC)
    service._app_probes = [
        *[
            _recovery_probe(intervention - timedelta(seconds=offset), impacted=True)
            for offset in (20, 15, 10, 5)
        ],
        *[
            _recovery_probe(
                intervention - timedelta(seconds=offset),
                impacted=True,
                probe_name="chat",
            )
            for offset in (20, 15, 10, 5)
        ],
        *[
            _recovery_probe(intervention + timedelta(seconds=offset), impacted=False)
            for offset in (5, 10, 15, 20)
        ],
    ]
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    with pytest.raises(PredictionWithheldError, match="missing post-intervention evidence"):
        service.close(
            service.incident_id,
            ClosureRequest(
                actor_id="operator-01",
                outcome="resolved",
                note="Recovery was not measured on the impacted stream.",
                intervention_at=intervention,
            ),
        )


def test_live_resolved_closure_requires_intervention_time(
    project_root: Path, monkeypatch
) -> None:
    service = DemoService(root=project_root)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    prediction_id = service.prediction().prediction_id
    monkeypatch.setattr(DemoService, "fixture_only", property(lambda _service: False))
    monkeypatch.setattr(service, "_require_workflow_ready", lambda: None)
    monkeypatch.setattr(service, "_require_prediction", lambda _prediction_id: prediction_id)
    with pytest.raises(PredictionWithheldError, match="requires an intervention time"):
        service.close(
            service.incident_id,
            ClosureRequest(
                actor_id="operator-01",
                outcome="resolved",
                note="Cannot claim recovery without measured transition evidence.",
            ),
        )


def test_case_history_survives_new_reviews_and_restart(project_root: Path, tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'cases.db').as_posix()}"
    store = PersistenceStore(url)
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    first = submit_case(service)
    second = submit_case(service, "A second closure submission with new checks.")
    assert first.id != second.id
    store.dispose()

    restarted_store = PersistenceStore(url)
    restarted = DemoService(root=project_root, store=restarted_store)
    cases = restarted.list_closure_cases()
    assert [case.id for case in cases] == [second.id, first.id]
    assert cases[1].feature_window == first.feature_window
    assert cases[1].incident == first.incident
    restarted.decide_closure_case(
        first.id,
        ClosureReviewRequest(
            actor_id="reviewer-01", decision="approved", note="Past case checked."
        ),
    )
    # Reviewing historical evidence must not approve the newer submission.
    assert restarted.incident().state == "validating"
    assert restarted.list_closure_cases(status="approved")[0].id == first.id
    assert restarted.list_closure_cases(status="pending")[0].id == second.id


def test_case_decision_is_final_and_atomic_with_audit(project_root: Path, monkeypatch) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    case = submit_case(service)
    request = ClosureReviewRequest(
        actor_id="reviewer-01", decision="approved", note="Independently checked."
    )
    original = store._add_audit_event

    def fail_audit(*_args):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(store, "_add_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        service.decide_closure_case(case.id, request)
    assert service.closure_case(case.id).review is None
    assert service.incident().state == "validating"
    monkeypatch.setattr(store, "_add_audit_event", original)
    service.decide_closure_case(case.id, request)
    assert service.incident().state == "resolved"
    with pytest.raises(ValueError, match="final review"):
        service.decide_closure_case(case.id, request.model_copy(update={"decision": "rejected"}))


def test_case_api_enforces_review_role_and_pagination(project_root: Path) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    case = submit_case(service)
    settings = Settings(
        environment="test", data_mode="replay", database_url="sqlite://", auth_mode="disabled"
    )
    client = TestClient(create_app(service, store=store, settings=settings))
    decision = {"actor_id": "reviewer-01", "decision": "approved", "note": "Evidence checked."}
    path = f"/api/cases/{case.id}/decision"
    assert client.post(path, json=decision).status_code == 403
    result = client.post(path, json=decision, headers={"X-Actor-Role": "reviewer"})
    assert result.status_code == 200
    assert result.json()["review"]["actor_id"] == "reviewer-01"
    assert client.get("/api/cases?status=approved&limit=1").json()[0]["id"] == case.id
    assert client.get("/api/cases?status=pending").json() == []
    assert client.get("/api/cases?outcome=resolved").json()[0]["id"] == case.id
    assert client.get("/api/cases?outcome=failed").json() == []
    assert (
        client.get(
            "/api/cases",
            params={"incident_type": case.incident.prediction.likely_incident_type},
        ).json()[0]["id"]
        == case.id
    )
    assert (
        client.get(
            "/api/cases", params={"entity_id": case.incident.prediction.entity_id}
        ).json()[0]["id"]
        == case.id
    )
    assert client.get("/api/cases?incident_type=unknown").status_code == 422
    assert client.get("/api/cases?entity_id=invalid%20entity").status_code == 422
    assert client.get("/api/cases?network_profile_sha256=invalid").status_code == 422
    assert client.get("/api/cases?limit=201").status_code == 422
    assert client.get("/api/cases?offset=-1").status_code == 422
    assert client.get("/api/cases/unknown").status_code == 404
