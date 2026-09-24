from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prahari.api import create_app
from prahari.contracts import ClosureRequest
from prahari.services.demo import DemoService
from prahari.settings import Settings


@pytest.mark.parametrize("operation", ["review", "dry-run", "approve", "closure"])
def test_stale_client_evidence_rejected_before_workflow_mutation(project_root: Path, operation):
    service = DemoService(root=project_root)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    before = service._audit_trail()
    client = TestClient(create_app(service, settings=Settings(environment="test")))
    path = f"/api/incidents/{service.incident_id}"
    payload = {
        "actor_id": "operator-01",
        "note": "Checked evidence.",
        "prediction_id": "pred-obsolete",
    }
    if operation in {"dry-run", "approve"}:
        path += f"/actions/{service.playbook_id}/{operation}"
    else:
        path += f"/{operation}"
    if operation == "closure":
        payload["outcome"] = "uncertain"
    response = client.post(path, json=payload)
    assert response.status_code == 409
    assert "evidence changed" in response.json()["detail"]
    assert service._audit_trail() == before
    assert service.list_closure_cases() == []


def test_matching_evidence_is_saved_in_case(project_root: Path):
    service = DemoService(root=project_root)
    prediction_id = service.prediction().prediction_id
    service.review(
        service.incident_id, "operator-01", "Checked evidence.", prediction_id=prediction_id
    )
    service.close(
        service.incident_id,
        ClosureRequest(
            actor_id="operator-01",
            prediction_id=prediction_id,
            outcome="uncertain",
            note="Escalated for investigation.",
        ),
    )
    case = service.list_closure_cases()[0]
    assert case.prediction_id == prediction_id
    assert case.submission.prediction_id == prediction_id


@pytest.mark.parametrize("operation", ["review", "dry-run", "approve", "closure"])
def test_production_requires_client_prediction_id(project_root: Path, operation, monkeypatch):
    service = DemoService(root=project_root)
    settings = Settings(environment="test")
    app = create_app(service, settings=settings)
    # Isolate the production request precondition without configuring live collectors.
    monkeypatch.setattr(settings, "environment", "production")
    client = TestClient(app)
    path = f"/api/incidents/{service.incident_id}"
    payload = {"actor_id": "operator-01", "note": "Checked evidence."}
    if operation in {"dry-run", "approve"}:
        path += f"/actions/{service.playbook_id}/{operation}"
    else:
        path += f"/{operation}"
    if operation == "closure":
        payload["outcome"] = "uncertain"
    response = client.post(path, json=payload)
    assert response.status_code == 428
    assert service._audit_trail() == []
