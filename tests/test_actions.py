import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from prahari.actions import (
    ActionAgentError,
    AgentActionRequest,
    AgentActionResponse,
    LabActionAgentClient,
)
from prahari.analytics.baseline import PredictionWithheldError
from prahari.api import create_app
from prahari.contracts import ActionOperation, ActionReconciliationRequest, ClosureRequest
from prahari.persistence import PersistenceStore
from prahari.services.demo import DemoService, PrahariService
from prahari.settings import Settings


def _agent_response(action_status: str, operation_id: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "status": action_status,
        "message": "fixed action result",
        "observed_route": "primary" if action_status != "applied" else "backup",
        "checked_at": "2026-09-01T18:00:00Z",
    }


def test_action_agent_client_uses_exact_origin_and_bearer_token(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("a" * 64, encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {'a' * 64}"
        assert request.url.path.endswith("/dry-run")
        operation_id = json.loads(request.content)["operation_id"]
        return httpx.Response(200, json=_agent_response("dry_run_passed", operation_id))

    client = LabActionAgentClient(
        "http://172.31.255.1:8091",
        token_file,
        allowed_private_ip="172.31.255.1",
        transport=httpx.MockTransport(handler),
    )
    response = client.dry_run(
        AgentActionRequest(
            operation_id="dry-run-client-test",
            incident_id=DemoService.incident_id,
            playbook_id=DemoService.playbook_id,
            actor_id="operator-01",
            prediction_id="pred-client-test",
        )
    )
    assert response.status == "dry_run_passed"
    assert response.operation_id == "dry-run-client-test"


def test_action_agent_client_rejects_uncorrelated_receipt(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("c" * 64, encoding="utf-8")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_agent_response("applied", "action-stale"))

    client = LabActionAgentClient(
        "http://172.31.255.1:8091",
        token_file,
        allowed_private_ip="172.31.255.1",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ActionAgentError, match="does not match"):
        client.apply(
            AgentActionRequest(
                operation_id="action-current",
                incident_id=DemoService.incident_id,
                playbook_id=DemoService.playbook_id,
                actor_id="operator-01",
                prediction_id="pred-client-test",
            )
        )


def test_action_agent_client_rejects_any_other_origin(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("b" * 64, encoding="utf-8")
    with pytest.raises(ValueError, match="exact allowed"):
        LabActionAgentClient(
            "http://172.31.255.2:8091",
            token_file,
            allowed_private_ip="172.31.255.1",
        )


class _FakeAgent:
    def dry_run(self, request: AgentActionRequest) -> AgentActionResponse:
        return AgentActionResponse(
            operation_id=request.operation_id,
            status="dry_run_passed",
            message="prerequisites passed",
            observed_route="primary",
            checked_at=datetime.now(UTC),
        )

    def apply(self, request: AgentActionRequest) -> AgentActionResponse:
        return AgentActionResponse(
            operation_id=request.operation_id,
            status="applied",
            message="backup selected",
            observed_route="backup",
            checked_at=datetime.now(UTC),
        )

    def rollback(self, request: AgentActionRequest) -> AgentActionResponse:
        return AgentActionResponse(
            operation_id=request.operation_id,
            status="rolled_back",
            message="primary selected",
            observed_route="primary",
            checked_at=datetime.now(UTC),
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"updated_at": datetime(2025, 1, 1, tzinfo=UTC)}, "precede its creation"),
        (
            {"status": "pending", "message": "uncommitted outcome"},
            "pending operation cannot contain outcome",
        ),
        (
            {"status": "unknown", "result_status": "applied", "observed_route": "backup"},
            "unknown operation cannot claim",
        ),
        (
            {"status": "finished", "result_status": "applied", "observed_route": "primary"},
            "observed backup route",
        ),
    ],
)
def test_action_operation_contract_rejects_inconsistent_outcome_evidence(
    project_root: Path,
    updates: dict[str, object],
    message: str,
) -> None:
    service = PrahariService(root=project_root)
    created_at = datetime(2026, 9, 1, tzinfo=UTC)
    value = {
        "id": "action-contract-test",
        "incident_id": service.incident_id,
        "playbook_id": service.playbook_id,
        "prediction_id": service.prediction().prediction_id,
        "prediction_snapshot": service.prediction(),
        "kind": "apply",
        "actor_id": "operator-01",
        "note": "Contract validation test.",
        "created_at": created_at,
        "updated_at": created_at,
        "status": "pending",
        **updates,
    }
    with pytest.raises(ValidationError, match=message):
        ActionOperation.model_validate(value)


def test_service_requires_review_then_audits_action_lifecycle(project_root: Path) -> None:
    service = PrahariService(root=project_root, action_agent=_FakeAgent())  # type: ignore[arg-type]

    with pytest.raises(PredictionWithheldError, match="review"):
        service.dry_run_action(service.incident_id, service.playbook_id, "operator-01")

    service.review(service.incident_id, "operator-01", "reviewed evidence")
    assert (
        service.dry_run_action(service.incident_id, service.playbook_id, "operator-01").status
        == "dry_run_passed"
    )
    assert (
        service.approve_action(
            service.incident_id, service.playbook_id, "operator-01", "approve"
        ).status
        == "applied"
    )
    assert (
        service.rollback_action(
            service.incident_id, service.playbook_id, "operator-01", "restore"
        ).status
        == "rolled_back"
    )
    assert [event.event_type for event in service.audit_trail(service.incident_id)][-5:] == [
        "action_dry_run_passed",
        "action_apply_requested",
        "action_applied",
        "rollback_requested",
        "action_rolled_back",
    ]


def test_action_requires_review_of_current_prediction_evidence(
    project_root: Path,
    monkeypatch,
) -> None:
    service = PrahariService(root=project_root, action_agent=_FakeAgent())  # type: ignore[arg-type]
    service.review(service.incident_id, "operator-01", "reviewed evidence")
    original_prediction = service.prediction

    def changed_prediction():
        return original_prediction().model_copy(update={"prediction_id": "pred-new-evidence"})

    monkeypatch.setattr(service, "prediction", changed_prediction)

    with pytest.raises(PredictionWithheldError, match="evidence changed"):
        service.dry_run_action(service.incident_id, service.playbook_id, "operator-01")


@pytest.mark.parametrize(
    ("operation", "agent_method", "intent_event"),
    [
        ("dry-run", "dry_run", None),
        ("approve", "apply", "action_apply_requested"),
        ("rollback", "rollback", "rollback_requested"),
    ],
)
def test_action_failure_returns_503_and_preserves_durable_intent(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
    operation: str,
    agent_method: str,
    intent_event: str | None,
) -> None:
    database_url = f"sqlite:///{(tmp_path / 'actions.db').as_posix()}"
    store = PersistenceStore(database_url)
    store.create_schema()
    agent = _FakeAgent()

    def fail_action(_request: AgentActionRequest) -> AgentActionResponse:
        raise ActionAgentError("action agent unavailable; outcome could not be confirmed")

    monkeypatch.setattr(agent, agent_method, fail_action)
    service = PrahariService(
        root=project_root,
        store=store,
        action_agent=agent,  # type: ignore[arg-type]
    )
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    settings = Settings(
        environment="test",
        data_mode="replay",
        database_url=database_url,
        auth_mode="disabled",
    )
    client = TestClient(
        create_app(service, settings=settings, store=store),
        raise_server_exceptions=False,
    )

    response = client.post(
        f"/api/incidents/{service.incident_id}/actions/{service.playbook_id}/{operation}",
        json={"actor_id": "operator-01", "note": "Operator requested action."},
    )
    assert response.status_code == 503
    assert "outcome could not be confirmed" in response.json()["detail"]
    store.dispose()

    restarted_store = PersistenceStore(database_url)
    events = restarted_store.list_audit(service.incident_id)
    assert events[-1].event_type == (
        "action_outcome_unknown" if intent_event else "incident_reviewed"
    )
    if intent_event:
        assert any(event.event_type == intent_event for event in events)
        assert restarted_store.list_actions(service.incident_id)[0].status == "unknown"
    assert not any(event.event_type in {"action_applied", "action_rolled_back"} for event in events)
    restarted_store.dispose()


@pytest.mark.parametrize("operation", ["approve_action", "rollback_action"])
def test_route_mutation_is_not_attempted_when_intent_cannot_be_persisted(
    project_root: Path, monkeypatch, operation: str
) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    agent = _FakeAgent()
    service = PrahariService(
        root=project_root,
        store=store,
        action_agent=agent,  # type: ignore[arg-type]
    )
    service.review(service.incident_id, "operator-01", "Reviewed evidence.")
    mutation_attempted = False

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit store unavailable")

    def mutate(_request):
        nonlocal mutation_attempted
        mutation_attempted = True
        raise AssertionError("external mutation must not run without durable intent")

    monkeypatch.setattr(store, "_add_audit_event", fail_audit)
    monkeypatch.setattr(agent, "apply", mutate)
    monkeypatch.setattr(agent, "rollback", mutate)

    with pytest.raises(RuntimeError, match="audit store unavailable"):
        getattr(service, operation)(
            service.incident_id, service.playbook_id, "operator-01", "Request route change."
        )

    assert mutation_attempted is False
    assert store.list_actions(service.incident_id) == []
    assert [event.event_type for event in store.list_audit(service.incident_id)] == [
        "incident_reviewed"
    ]


def _reconciliation(actor: str = "reviewer-02") -> ActionReconciliationRequest:
    return ActionReconciliationRequest(
        actor_id=actor,
        note="Workers and device agent stopped; primary route independently verified.",
        evidence_reference="offline-evidence/route-inspection-001",
        evidence_sha256="e" * 64,
        dispatch_quiesced=True,
        observed_route="primary",
    )


def test_committed_action_cannot_be_dispatched_again_after_restart(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{(tmp_path / 'ledger.db').as_posix()}"
    store = PersistenceStore(url)
    store.create_schema()
    agent = _FakeAgent()
    original_apply = agent.apply
    calls = []

    def apply(request):
        calls.append(request)
        return original_apply(request)

    monkeypatch.setattr(agent, "apply", apply)
    service = PrahariService(root=project_root, store=store, action_agent=agent)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    assert (
        service.approve_action(
            service.incident_id, service.playbook_id, "operator-01", "Approve once."
        ).status
        == "applied"
    )
    store.dispose()
    restarted_store = PersistenceStore(url)
    restarted = PrahariService(root=project_root, store=restarted_store, action_agent=agent)
    with pytest.raises(ValueError, match="already registered"):
        restarted.approve_action(
            service.incident_id, service.playbook_id, "operator-01", "Repeated request."
        )
    assert len(calls) == 1
    assert len(restarted.list_action_operations()) == 1
    assert restarted.has_unconfirmed_action() is False
    restarted_store.dispose()


def test_result_commit_failure_preserves_pending_claim_and_prevents_retry(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{(tmp_path / 'commit-failure.db').as_posix()}"
    store = PersistenceStore(url)
    store.create_schema()
    service = PrahariService(root=project_root, store=store, action_agent=_FakeAgent())
    service.review(service.incident_id, "operator-01", "Reviewed evidence.")

    original_audit = store._add_audit_event

    def fail_result_audit(session, incident_id, event):
        if event.event_type == "action_applied":
            raise RuntimeError("storage unavailable after external success")
        return original_audit(session, incident_id, event)

    monkeypatch.setattr(store, "_add_audit_event", fail_result_audit)
    with pytest.raises(ActionAgentError, match="could not be committed"):
        service.approve_action(
            service.incident_id, service.playbook_id, "operator-01", "Attempt route shift."
        )
    assert service.list_action_operations()[0].status == "pending"
    assert service.has_unconfirmed_action()
    with pytest.raises(ValueError, match="unconfirmed"):
        service.rollback_action(
            service.incident_id, service.playbook_id, "operator-01", "Unsafe automatic retry."
        )
    store.dispose()


@pytest.mark.parametrize("agent_status", ["validation_failed", "dry_run_passed", "wrong-route"])
def test_unconfirmed_result_blocks_workflow_until_independent_monitoring_mode_reconciliation(
    project_root: Path,
    agent_status: str,
    monkeypatch,
) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    agent = _FakeAgent()

    def unconfirmed(request):
        return AgentActionResponse(
            operation_id=request.operation_id,
            status="applied" if agent_status == "wrong-route" else agent_status,
            message="Route outcome needs verification.",
            observed_route="primary",
            checked_at=datetime.now(UTC),
        )

    monkeypatch.setattr(agent, "apply", unconfirmed)
    service = PrahariService(root=project_root, store=store, action_agent=agent)
    service.review(service.incident_id, "operator-01", "Reviewed evidence.")
    if agent_status == "validation_failed":
        assert (
            service.approve_action(
                service.incident_id, service.playbook_id, "operator-01", "Attempt shift."
            ).status
            == "validation_failed"
        )
    else:
        with pytest.raises(ActionAgentError):
            service.approve_action(
                service.incident_id, service.playbook_id, "operator-01", "Attempt shift."
            )
    operation = service.list_action_operations()[0]
    assert operation.status == "unknown"
    assert operation.prediction_snapshot.prediction_id == operation.prediction_id
    assert service.incident().state == "escalated"
    with pytest.raises(ValueError, match="unconfirmed"):
        service.review(service.incident_id, "operator-01", "Must not erase action uncertainty.")
    with pytest.raises(ValueError, match="unconfirmed"):
        service.close(
            service.incident_id,
            ClosureRequest(
                actor_id="operator-01", outcome="resolved", note="Must not falsely close recovery."
            ),
        )
    with pytest.raises(ValueError, match="disable the device adapter"):
        service.reconcile_action(operation.id, _reconciliation())
    monitoring = PrahariService(root=project_root, store=store)
    with pytest.raises(ValueError, match="independent reviewer"):
        monitoring.reconcile_action(operation.id, _reconciliation("operator-01"))
    verified = _reconciliation().model_copy(update={"observed_route": "backup"})
    reconciled = monitoring.reconcile_action(operation.id, verified)
    assert reconciled.status == "finished"
    assert reconciled.result_status != "applied"
    assert reconciled.reconciliation.evidence_sha256 == "e" * 64
    assert reconciled.observed_route == "backup"
    assert reconciled.agent_receipt.observed_route == "primary"
    assert monitoring.has_unconfirmed_action() is False
    with pytest.raises(ValueError, match="already finalized"):
        monitoring.reconcile_action(operation.id, _reconciliation("reviewer-03"))
    store.dispose()


def test_orphaned_pending_action_is_visible_without_telemetry_and_reconciliation_is_role_gated(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{(tmp_path / 'orphan.db').as_posix()}"
    store = PersistenceStore(url)
    store.create_schema()
    service = PrahariService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed before simulated crash.")
    operation = service._begin_operation(
        "apply", "operator-01", "Simulated worker crash.", service.prediction().prediction_id
    )
    store.dispose()
    store = PersistenceStore(url)
    monitoring = PrahariService(root=project_root, data_mode="live", store=store)
    monkeypatch.setattr(monitoring, "readiness", lambda: (False, "telemetry unavailable"))
    settings = Settings(
        environment="test", data_mode="live", database_url=url, auth_mode="disabled"
    )
    client = TestClient(create_app(monitoring, settings=settings, store=store))
    assert client.get("/api/incidents").status_code == 503
    page = client.get("/api/action-operations")
    assert page.status_code == 200
    assert page.json()["operations"][0]["id"] == operation.id
    assert page.json()["dispatch_enabled"] is False
    body = _reconciliation().model_dump(mode="json")
    endpoint = f"/api/action-operations/{operation.id}/reconcile"
    assert client.post(endpoint, json=body).status_code == 403
    assert client.post(endpoint, json=body, headers={"X-Actor-Role": "operator"}).status_code == 403
    assert (
        client.post(
            endpoint,
            json={**body, "dispatch_quiesced": False},
            headers={"X-Actor-Role": "reviewer"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            endpoint,
            json={**body, "evidence_sha256": "0" * 64},
            headers={"X-Actor-Role": "reviewer"},
        ).status_code
        == 422
    )
    response = client.post(endpoint, json=body, headers={"X-Actor-Role": "reviewer"})
    assert response.status_code == 200, response.text
    assert response.json()["result_status"] is None
    assert store.has_unconfirmed_action(service.incident_id) is False
    store.dispose()
