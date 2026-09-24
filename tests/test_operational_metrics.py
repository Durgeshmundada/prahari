from fastapi.testclient import TestClient

from prahari.api import create_app
from prahari.persistence import PersistenceStore
from prahari.services.demo import DemoService
from prahari.settings import Settings


def test_metric_labels_bound_methods_and_scrape_requires_viewer_auth(project_root):
    client = TestClient(
        create_app(
            service=DemoService(root=project_root),
            settings=Settings(
                auth_mode="api_key",
                api_keys_json='{"test-only-token":{"actor_id":"monitor","role":"viewer"}}',
            ),
        )
    )
    client.request("HOSTILE_METHOD_123", "/unknown-client-path-123")
    assert client.get("/metrics").status_code == 401
    response = client.get("/metrics", headers={"Authorization": "Bearer test-only-token"})
    assert response.status_code == 200
    assert 'method="OTHER",path="unmatched"' in response.text
    assert "HOSTILE_METHOD_123" not in response.text
    assert "unknown-client-path-123" not in response.text
    assert "prahari_http_request_duration_seconds" in response.text


def test_unconfirmed_action_visible_in_metrics_and_storage_failure_is_not_zero(
    project_root, monkeypatch
):
    store = PersistenceStore("sqlite:///:memory:")
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service._begin_operation(
        "apply", "operator-01", "Pending test intent.", service.prediction().prediction_id
    )
    client = TestClient(create_app(service=service))
    response = client.get("/metrics")
    assert "prahari_unconfirmed_actions 1.0" in response.text
    assert "prahari_database_available 1.0" in response.text
    monkeypatch.setattr(
        store, "healthcheck", lambda: (_ for _ in ()).throw(RuntimeError("secret detail"))
    )
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "prahari_database_available 0.0" in response.text
    assert "prahari_unconfirmed_actions NaN" in response.text
    assert "secret detail" not in response.text
    store.dispose()
