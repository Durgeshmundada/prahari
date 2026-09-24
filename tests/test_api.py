from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from prometheus_client import generate_latest

from prahari.api import app_for_root, create_app
from prahari.persistence import PersistenceStore
from prahari.services.demo import DemoService, PrahariService
from prahari.settings import Settings


def test_api_exposes_replay_incident_and_blocks_unconfigured_action(project_root: Path) -> None:
    client = TestClient(app_for_root(project_root))

    topology = client.get("/api/topology")
    incidents = client.get("/api/incidents")

    assert topology.status_code == 200
    assert topology.json()["fixture_only"] is True
    assert incidents.status_code == 200
    incident = incidents.json()[0]
    assert incident["state"] == "predicted"
    assert incident["prediction"]["likely_incident_type"] == "tunnel_degradation"

    review = client.post(
        f"/api/incidents/{DemoService.incident_id}/review",
        json={"actor_id": "operator-01", "note": "Evidence reviewed."},
    )
    dry_run = client.post(
        f"/api/incidents/{DemoService.incident_id}/actions/{DemoService.playbook_id}/dry-run",
        json={"actor_id": "operator-01", "note": "Request validation."},
    )
    approval = client.post(
        f"/api/incidents/{DemoService.incident_id}/actions/{DemoService.playbook_id}/approve",
        json={"actor_id": "operator-01", "note": "Attempt approval."},
    )

    assert review.status_code == 200
    assert review.json()["state"] == "reviewed"
    assert dry_run.status_code == 200
    assert dry_run.json()["status"] == "blocked"
    assert approval.status_code == 409
    assert approval.json()["status"] == "blocked"


def test_prometheus_path_labels_do_not_include_unmatched_request_paths(project_root: Path) -> None:
    client = TestClient(app_for_root(project_root))

    client.get("/arbitrary-client-controlled-value-1")
    client.get("/arbitrary-client-controlled-value-2")
    metrics = generate_latest().decode("utf-8")

    assert 'path="unmatched"' in metrics
    assert "arbitrary-client-controlled-value" not in metrics


def test_app_probe_listing_is_bounded_and_rejects_an_excessive_limit(project_root: Path) -> None:
    client = TestClient(app_for_root(project_root))

    bounded = client.get("/api/app-probes?limit=1")
    excessive = client.get("/api/app-probes?limit=5001")

    assert bounded.status_code == 200
    assert len(bounded.json()) == 1
    assert excessive.status_code == 422


def test_traffic_demo_summarises_a_durable_verified_transfer(project_root: Path) -> None:
    settings = Settings(
        environment="test",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    client = TestClient(
        create_app(
            DemoService(root=project_root, store=store),
            settings=settings,
            store=store,
        )
    )

    idle = client.get("/api/demo/traffic")
    assert idle.status_code == 200
    assert idle.json()["status"] == "idle"

    payload = {
        "run_id": "two-server-demo-01",
        "records": [
            {
                "observed_at": "2026-08-20T09:01:00Z",
                "entity_id": "app:file-upload:branch-01-to-dc",
                "probe_name": "file-upload",
                "packet_loss_ratio": 0.0,
                "jitter_ms": 2.5,
                "request_latency_ms": 42.0,
                "path_id": "path:branch-01-to-dc:primary",
                "topology_version": "lab-v1",
                "config_version": "lab-config-v1",
                "quality": {"source_healthy": True},
                "transfer": {
                    "transfer_id": "demo-transfer-001",
                    "direction": "branch_to_dc",
                    "protocol": "websocket",
                    "content_type": "image/jpeg",
                    "file_name": "network-demo-photo.jpg",
                    "kind": "photo",
                    "bytes_expected": 1024,
                    "bytes_transferred": 1024,
                    "bytes_received": 1024,
                    "status": "completed",
                    "checksum_sha256": "a" * 64,
                    "integrity": "verified",
                    "elapsed_ms": 100.0,
                    "chat_measurements": {
                        "messages_attempted": 10,
                        "messages_received": 10,
                        "rtt_min_ms": 10.0,
                        "rtt_p50_ms": 20.0,
                        "rtt_p95_ms": 30.0,
                        "rtt_max_ms": 40.0,
                        "jitter_ms": 2.0,
                    },
                },
            }
        ],
    }
    accepted = client.post(
        "/api/telemetry/app-probes",
        json=payload,
        headers={"X-Actor-Role": "collector"},
    )

    assert accepted.status_code == 202
    response = client.get("/api/demo/traffic")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["path_id"] == "path:branch-01-to-dc:primary"
    assert body["source"] == {"id": "site:branch-01", "name": "Branch-01"}
    assert body["destination"] == {"id": "site:dc-01", "name": "DC-01"}
    assert body["transport"] == "websocket"
    assert body["payload"] == {
        "name": "network-demo-photo.jpg",
        "kind": "photo",
        "size_bytes": 1024,
        "bytes_sent": 1024,
        "bytes_received": 1024,
        "sha256": "a" * 64,
        "integrity": "verified",
    }
    assert body["latency_ms"] == {"min": 10.0, "p50": 20.0, "p95": 30.0, "max": 40.0}
    assert body["throughput_mbps"] == 0.082
    assert body["loss_percent"] == 0.0
    assert body["trace"][0]["stage"] == "chat_probe"
    assert body["trace"][-1]["status"] == "passed"
    assert "transfer_id" not in body

    reloaded = TestClient(
        create_app(
            DemoService(root=project_root, store=store),
            settings=settings,
            store=store,
        )
    )
    assert reloaded.get("/api/demo/traffic").json()["status"] == "passed"

    bad_path = client.get("/api/demo/traffic?path_id=path:not-configured")
    assert bad_path.status_code == 404


def test_traffic_demo_fails_whole_demo_when_chat_delivery_is_incomplete(project_root: Path) -> None:
    client = TestClient(app_for_root(project_root))
    payload = {
        "records": [
            {
                "observed_at": "2026-08-20T09:01:00Z",
                "entity_id": "app:file-upload:branch-01-to-dc",
                "probe_name": "file-upload",
                "packet_loss_ratio": 0.0,
                "jitter_ms": 0.0,
                "request_latency_ms": 0.0,
                "path_id": "path:branch-01-to-dc:primary",
                "topology_version": "lab-v1",
                "config_version": "lab-config-v1",
                "quality": {"source_healthy": True},
                "transfer": {
                    "transfer_id": "demo-transfer-chat-loss",
                    "direction": "branch_to_dc",
                    "protocol": "websocket",
                    "content_type": "application/octet-stream",
                    "file_name": "demo.bin",
                    "kind": "file",
                    "bytes_expected": 1024,
                    "bytes_transferred": 1024,
                    "bytes_received": 1024,
                    "status": "completed",
                    "checksum_sha256": "c" * 64,
                    "integrity": "verified",
                    "chat_measurements": {
                        "messages_attempted": 4,
                        "messages_received": 3,
                        "rtt_min_ms": 10.0,
                        "rtt_p50_ms": 12.0,
                        "rtt_p95_ms": 20.0,
                        "rtt_max_ms": 22.0,
                        "jitter_ms": 1.0,
                    },
                },
            }
        ]
    }

    accepted = client.post(
        "/api/telemetry/app-probes",
        json=payload,
        headers={"X-Actor-Role": "collector"},
    )
    response = client.get("/api/demo/traffic")

    assert accepted.status_code == 202
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["payload"]["integrity"] == "verified"
    assert body["loss_percent"] == 25.0
    assert body["trace"][0]["status"] == "failed"
    assert "chat delivery was incomplete" in body["message"]


def test_traffic_demo_rejects_an_unverified_completed_transfer(project_root: Path) -> None:
    client = TestClient(app_for_root(project_root))
    malformed = {
        "records": [
            {
                "observed_at": "2026-08-20T09:01:00Z",
                "entity_id": "app:file-upload:branch-01-to-dc",
                "probe_name": "file-upload",
                "packet_loss_ratio": 0.0,
                "jitter_ms": 2.5,
                "request_latency_ms": 42.0,
                "path_id": "path:branch-01-to-dc:primary",
                "topology_version": "lab-v1",
                "config_version": "lab-config-v1",
                "quality": {"source_healthy": True},
                "transfer": {
                    "transfer_id": "demo-transfer-002",
                    "direction": "branch_to_dc",
                    "protocol": "websocket",
                    "content_type": "video/mp4",
                    "file_name": "demo.mp4",
                    "kind": "video",
                    "bytes_expected": 1024,
                    "bytes_transferred": 1024,
                    "status": "completed",
                    "checksum_sha256": "a" * 64,
                    "integrity": "verified",
                },
            }
        ]
    }

    response = client.post(
        "/api/telemetry/app-probes",
        json=malformed,
        headers={"X-Actor-Role": "collector"},
    )

    assert response.status_code == 422


def test_live_traffic_demo_expires_old_transfer_success(project_root: Path) -> None:
    settings = Settings(
        environment="test",
        data_mode="live",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    client = TestClient(
        create_app(
            PrahariService(root=project_root, data_mode="live", store=store),
            settings=settings,
            store=store,
        )
    )
    expired_at = (datetime.now(UTC) - timedelta(seconds=121)).isoformat()
    payload = {
        "records": [
            {
                "observed_at": expired_at,
                "entity_id": "app:file-upload:branch-01-to-dc",
                "probe_name": "file-upload",
                "packet_loss_ratio": 0.0,
                "jitter_ms": 2.5,
                "request_latency_ms": 42.0,
                "path_id": "path:branch-01-to-dc:primary",
                "topology_version": "lab-v1",
                "config_version": "lab-config-v1",
                "quality": {"source_healthy": True},
                "transfer": {
                    "transfer_id": "expired-transfer-001",
                    "direction": "branch_to_dc",
                    "protocol": "http",
                    "content_type": "application/octet-stream",
                    "file_name": "expired-demo.bin",
                    "kind": "file",
                    "bytes_expected": 1024,
                    "bytes_transferred": 1024,
                    "bytes_received": 1024,
                    "status": "completed",
                    "checksum_sha256": "b" * 64,
                    "integrity": "verified",
                },
            }
        ]
    }

    accepted = client.post(
        "/api/telemetry/app-probes",
        json=payload,
        headers={"X-Actor-Role": "collector"},
    )
    response = client.get("/api/demo/traffic")

    assert accepted.status_code == 202
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert "freshness limit" in response.json()["message"]


def test_live_api_surfaces_unready_telemetry_instead_of_an_empty_healthy_noc(
    project_root: Path,
) -> None:
    settings = Settings(
        environment="test",
        data_mode="live",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    client = TestClient(
        create_app(
            PrahariService(root=project_root, data_mode="live", store=store),
            settings=settings,
            store=store,
        )
    )

    health = client.get("/health")
    incidents = client.get("/api/incidents")

    assert health.status_code == 200
    assert health.json()["status"] == "degraded"
    assert incidents.status_code == 503


def test_prediction_failure_is_explicit_for_incident_list_and_detail(
    project_root: Path,
    monkeypatch,
) -> None:
    service = DemoService(root=project_root)

    def fail_prediction():
        raise ValueError("model evidence is unusable")

    monkeypatch.setattr(service, "prediction", fail_prediction)
    settings = Settings(
        environment="test",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    client = TestClient(
        create_app(service, settings=settings),
        raise_server_exceptions=False,
    )

    incident_list = client.get("/api/incidents")
    incident_detail = client.get(f"/api/incidents/{service.incident_id}")

    assert incident_list.status_code == 503
    assert incident_list.json()["detail"] == "model evidence is unusable"
    assert incident_detail.status_code == 503
    assert incident_detail.json()["detail"] == "model evidence is unusable"


def test_resolved_closure_requires_review_and_independent_approval(project_root: Path) -> None:
    client = TestClient(app_for_root(project_root))
    incident_id = DemoService.incident_id
    closure = {
        "actor_id": "operator-01",
        "outcome": "resolved",
        "note": "Application checks show no current impact.",
    }

    unreviewed = client.post(f"/api/incidents/{incident_id}/closure", json=closure)
    assert unreviewed.status_code == 503

    reviewed = client.post(
        f"/api/incidents/{incident_id}/review",
        json={"actor_id": "operator-01", "note": "Reviewed current prediction evidence."},
    )
    assert reviewed.status_code == 200

    submitted = client.post(f"/api/incidents/{incident_id}/closure", json=closure)
    assert submitted.status_code == 200
    assert submitted.json()["state"] == "validating"

    operator_approval = client.post(
        f"/api/cases/{incident_id}/review",
        json={"actor_id": "operator-01", "decision": "approved", "note": "Self approval."},
    )
    assert operator_approval.status_code == 403
    self_approval = client.post(
        f"/api/cases/{incident_id}/review",
        headers={"X-Actor-Role": "reviewer"},
        json={"actor_id": "operator-01", "decision": "approved", "note": "Self approval."},
    )
    assert self_approval.status_code == 409

    approved = client.post(
        f"/api/cases/{incident_id}/review",
        headers={"X-Actor-Role": "reviewer", "X-Actor-Id": "reviewer-01"},
        json={
            "actor_id": "reviewer-01",
            "decision": "approved",
            "note": "Closure evidence independently reviewed.",
        },
    )
    assert approved.status_code == 200
    assert approved.json()["state"] == "resolved"


def test_audit_remains_available_when_live_predictions_are_withheld(
    project_root: Path,
) -> None:
    settings = Settings(
        environment="test",
        data_mode="live",
        database_url="sqlite://",
        auth_mode="disabled",
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    service = PrahariService(root=project_root, data_mode="live", store=store)
    store.append_audit(
        incident_id=service.incident_id,
        actor_id="operator-01",
        event_type="historical_review",
        detail="Evidence must remain readable during collector outage.",
    )
    client = TestClient(create_app(service, settings=settings, store=store))

    response = client.get(
        f"/api/audit/{service.incident_id}",
        headers={"X-Actor-Role": "reviewer"},
    )

    assert response.status_code == 200
    assert response.json()[0]["event_type"] == "historical_review"


def test_audit_pages_return_recent_events_and_reject_unbounded_requests(project_root: Path) -> None:
    settings = Settings(
        environment="test", data_mode="replay", database_url="sqlite://", auth_mode="disabled"
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    for index in range(5):
        store.append_audit(
            incident_id=service.incident_id,
            actor_id="operator-01",
            event_type=f"event_{index}",
            detail="Recorded workflow evidence.",
        )
    client = TestClient(create_app(service, settings=settings, store=store))
    headers = {"X-Actor-Role": "reviewer"}
    path = f"/api/audit/{service.incident_id}"

    newest = client.get(path, params={"limit": 2}, headers=headers)
    older = client.get(path, params={"limit": 2, "offset": 2}, headers=headers)

    assert newest.status_code == older.status_code == 200
    assert [event["event_type"] for event in newest.json()] == ["event_3", "event_4"]
    assert [event["event_type"] for event in older.json()] == ["event_1", "event_2"]
    assert client.get(path, params={"limit": 1001}, headers=headers).status_code == 422
    assert client.get(path, params={"limit": 0}, headers=headers).status_code == 422
    assert client.get(path, params={"offset": -1}, headers=headers).status_code == 422


def test_viewer_cannot_generate_briefs_or_approve_closure(project_root: Path) -> None:
    client = TestClient(app_for_root(project_root))
    headers = {"X-Actor-Role": "viewer"}
    incident_id = DemoService.incident_id

    brief = client.post(f"/api/incidents/{incident_id}/brief", headers=headers)
    approval = client.post(
        f"/api/cases/{incident_id}/review",
        headers=headers,
        json={"actor_id": "viewer-01", "decision": "approved", "note": "Approve closure."},
    )

    assert brief.status_code == 403
    assert approval.status_code == 403
