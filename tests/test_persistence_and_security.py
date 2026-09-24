import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text

from alembic import command
from prahari.analytics.baseline import PredictionWithheldError
from prahari.api import create_app
from prahari.contracts import ActionOperation, ClosureRequest, ClosureReviewRequest, EventRecord
from prahari.persistence import IncidentStateRow, PersistenceStore
from prahari.services.demo import DemoService, PrahariService
from prahari.settings import Settings


def test_existing_database_requires_explicit_action_migration_before_auto_schema_startup(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{(tmp_path / 'legacy-development.db').as_posix()}"
    monkeypatch.delenv("PRAHARI_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("PRAHARI_DATABASE_URL", url)
    config = Config(str(project_root / "alembic.ini"))
    command.upgrade(config, "0003_browser_sessions")
    store = PersistenceStore(url)
    with pytest.raises(RuntimeError, match="requires migration 0004_action_operations"):
        store.create_schema()
    assert store.schema_is_current() is False
    command.upgrade(config, "0004_action_operations")
    with pytest.raises(RuntimeError, match="requires migration 0005_case_search"):
        store.create_schema()
    command.upgrade(config, "head")
    store.create_schema()
    assert store.schema_is_current()
    store.dispose()


def test_case_search_migration_backfills_and_indexes_existing_payloads(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{(tmp_path / 'case-search.db').as_posix()}"
    monkeypatch.delenv("PRAHARI_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("PRAHARI_DATABASE_URL", url)
    config = Config(str(project_root / "alembic.ini"))
    command.upgrade(config, "0004_action_operations")
    payload = {
        "submission": {"outcome": "uncertain"},
        "incident": {
            "prediction": {
                "likely_incident_type": "tunnel_degradation",
                "entity_id": "tunnel:test",
                "network_profile_sha256": "a" * 64,
            }
        },
    }
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO closure_cases "
                "(id, incident_id, prediction_id, created_at, status, payload) "
                "VALUES (:id, :incident, :prediction, :created, :status, :payload)"
            ),
            {
                "id": "case-existing",
                "incident": "incident-existing",
                "prediction": "prediction-existing",
                "created": "2026-09-20T00:00:00+00:00",
                "status": "approved",
                "payload": json.dumps(payload),
            },
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT outcome, incident_type, entity_id, network_profile_sha256 "
                "FROM closure_cases WHERE id='case-existing'"
            )
        ).one()
    assert tuple(row) == ("uncertain", "tunnel_degradation", "tunnel:test", "a" * 64)
    index_names = {item["name"] for item in inspect(engine).get_indexes("closure_cases")}
    assert {
        "ix_closure_cases_outcome",
        "ix_closure_cases_incident_type",
        "ix_closure_cases_entity_id",
        "ix_closure_cases_network_profile_sha256",
    }.issubset(index_names)
    nullable = {
        item["name"]: item["nullable"] for item in inspect(engine).get_columns("closure_cases")
    }
    assert nullable["outcome"] is False
    assert nullable["incident_type"] is False
    assert nullable["entity_id"] is False
    assert nullable["network_profile_sha256"] is True
    engine.dispose()

    command.downgrade(config, "0004_action_operations")
    engine = create_engine(url)
    downgraded_columns = {
        item["name"] for item in inspect(engine).get_columns("closure_cases")
    }
    assert {
        "outcome",
        "incident_type",
        "entity_id",
        "network_profile_sha256",
    }.isdisjoint(downgraded_columns)
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        restored = connection.execute(
            text(
                "SELECT outcome, incident_type, entity_id, network_profile_sha256 "
                "FROM closure_cases WHERE id='case-existing'"
            )
        ).one()
    assert tuple(restored) == ("uncertain", "tunnel_degradation", "tunnel:test", "a" * 64)
    engine.dispose()


def test_closure_case_reads_reject_normalized_metadata_drift(
    project_root: Path,
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{(tmp_path / 'case-integrity.db').as_posix()}"
    store = PersistenceStore(url)
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(
            actor_id="operator-01",
            outcome="uncertain",
            note="Escalated pending further measurements.",
        ),
    )
    case_id = service.list_closure_cases()[0].id
    with store._engine.begin() as connection:
        connection.execute(
            text("UPDATE closure_cases SET entity_id='tunnel:tampered' WHERE id=:id"),
            {"id": case_id},
        )

    with pytest.raises(ValueError, match="normalized metadata.*entity_id"):
        store.closure_case(case_id)
    with pytest.raises(ValueError, match="normalized metadata.*entity_id"):
        store.list_closure_cases()
    store.dispose()


def test_action_reads_reject_normalized_metadata_drift(
    project_root: Path,
    tmp_path: Path,
) -> None:
    url = f"sqlite:///{(tmp_path / 'action-integrity.db').as_posix()}"
    store = PersistenceStore(url)
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    prediction = service.prediction()
    service.review(
        service.incident_id,
        "operator-01",
        "Reviewed current prediction evidence.",
    )
    created_at = datetime.now(UTC)
    operation = ActionOperation(
        id="action-integrity-test",
        incident_id=service.incident_id,
        playbook_id=service.playbook_id,
        prediction_id=prediction.prediction_id,
        prediction_snapshot=prediction,
        kind="apply",
        actor_id="operator-01",
        note="Persist intent for integrity validation.",
        created_at=created_at,
        updated_at=created_at,
        status="pending",
    )
    store.begin_action(operation)
    with store._engine.begin() as connection:
        connection.execute(
            text("UPDATE action_operations SET status='finished' WHERE id=:id"),
            {"id": operation.id},
        )

    with pytest.raises(ValueError, match="normalized metadata.*status"):
        store.list_actions(service.incident_id)
    store.dispose()


def test_integrity_verifier_rejects_unowned_unconfirmed_action(
    project_root: Path,
) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    prediction = service.prediction()
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    created_at = datetime.now(UTC)
    operation = ActionOperation(
        id="action-relationship-test",
        incident_id=service.incident_id,
        playbook_id=service.playbook_id,
        prediction_id=prediction.prediction_id,
        prediction_snapshot=prediction,
        kind="apply",
        actor_id="operator-01",
        note="Persist intent for relationship validation.",
        created_at=created_at,
        updated_at=created_at,
        status="pending",
    )
    store.begin_action(operation)
    assert store.verify_evidence_integrity()["incident_states"] == 1

    with store._engine.begin() as connection:
        connection.execute(
            text("UPDATE incident_states SET active_action_id=NULL WHERE incident_id=:id"),
            {"id": service.incident_id},
        )
    with pytest.raises(ValueError, match="action operation relationship.*incident_claim"):
        store.verify_evidence_integrity()
    store.dispose()


def test_integrity_verifier_rejects_missing_current_closure_case(
    project_root: Path,
) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(
            actor_id="operator-01",
            outcome="uncertain",
            note="Escalated pending further measurements.",
        ),
    )
    assert store.verify_evidence_integrity()["incident_states"] == 1

    with store._engine.begin() as connection:
        payload = connection.execute(
            text("SELECT closure_payload FROM incident_states WHERE incident_id=:id"),
            {"id": service.incident_id},
        ).scalar_one()
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload["case_id"] = "missing-case"
    with store._session_factory.begin() as session:
        row = session.get(IncidentStateRow, service.incident_id)
        assert row is not None
        row.closure_payload = payload
    with pytest.raises(ValueError, match="incident state relationship.*closure_case_exists"):
        store.verify_evidence_integrity()
    store.dispose()


def test_action_migration_refuses_to_discard_uncorrelated_legacy_device_intents(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    url = f"sqlite:///{(tmp_path / 'legacy-device-intent.db').as_posix()}"
    monkeypatch.delenv("PRAHARI_DATABASE_URL_FILE", raising=False)
    monkeypatch.setenv("PRAHARI_DATABASE_URL", url)
    config = Config(str(project_root / "alembic.ini"))
    command.upgrade(config, "0003_browser_sessions")
    store = PersistenceStore(url)
    store.append_audit(
        incident_id="test-legacy-incident",
        actor_id="test-operator",
        event_type="action_apply_requested",
        detail="Uncorrelated legacy test intent.",
    )
    with pytest.raises(RuntimeError, match="legacy device intents"):
        command.upgrade(config, "head")
    assert store.schema_is_current() is False
    assert store.list_audit("test-legacy-incident")[0].event_type == "action_apply_requested"
    store.dispose()


class _UnmigratedStore:
    def schema_is_current(self) -> bool:
        return False


def test_review_and_audit_survive_a_service_restart(project_root: Path) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    first = DemoService(root=project_root, store=store)

    first.review(first.incident_id, "operator-01", "Reviewed worsening loss.")

    restarted = DemoService(root=project_root, store=store)
    incident = restarted.incident()
    detail = restarted.incident_detail(restarted.incident_id)

    assert incident.state == "reviewed"
    assert detail["audit_trail"][0].actor_id == "operator-01"


def test_replay_startup_replaces_an_older_fixture_revision(project_root: Path) -> None:
    fixture = DemoService(root=project_root)
    fixture_metrics = fixture.metric_records(limit=5_000)
    fixture_probes = fixture.app_probe_records(limit=5_000)
    replay_run_id = "replay-gradual-loss-001"
    store = PersistenceStore("sqlite://")
    store.create_schema()

    stale_metric = fixture_metrics[0].model_copy(
        update={
            "quality": fixture_metrics[0].quality.model_copy(update={"late_by_ms": 1})
        }
    )
    stale_probe = fixture_probes[0].model_copy(
        update={
            "quality": fixture_probes[0].quality.model_copy(update={"late_by_ms": 1})
        }
    )
    unrelated_metric = fixture_metrics[0].model_copy(
        update={"observed_at": fixture_metrics[0].observed_at - timedelta(days=365)}
    )
    unrelated_probe = fixture_probes[0].model_copy(
        update={"observed_at": fixture_probes[0].observed_at - timedelta(days=365)}
    )
    store.save_metrics([stale_metric], run_id=replay_run_id)
    store.save_app_probes([stale_probe], run_id=replay_run_id)
    store.save_metrics([unrelated_metric], run_id="external-evidence")
    store.save_app_probes([unrelated_probe], run_id="external-evidence")

    restarted = DemoService(root=project_root, store=store)

    assert len(store.list_metrics(limit=5_000)) == len(fixture_metrics) + 1
    assert len(store.list_app_probes(limit=5_000)) == len(fixture_probes) + 1
    assert restarted.incident().prediction.prediction_id


def test_review_state_rolls_back_if_its_audit_insert_fails() -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()

    def fail_audit_insert(*_args, **_kwargs) -> None:
        raise RuntimeError("audit store unavailable")

    store._add_audit_event = fail_audit_insert  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="audit store unavailable"):
        store.review_with_audit("incident-01", "operator-01", "reviewed", "prediction-01")

    state = store.incident_state("incident-01")
    assert state.reviewed_by is None


def test_persistence_keeps_identical_records_from_distinct_runs(project_root: Path) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    record = DemoService(root=project_root).metric_records()[0]

    store.save_metrics([record], run_id="run-a")
    store.save_metrics([record], run_id="run-b")

    assert len(store.list_metrics()) == 2
    assert store.verify_evidence_integrity()["telemetry_records"] == 2


def test_evidence_reads_reject_content_id_and_metadata_drift(project_root: Path) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    metric = DemoService(root=project_root).metric_records()[0]
    store.save_metrics([metric], run_id="integrity-run")
    audit = store.append_audit(
        incident_id="incident-integrity",
        actor_id="operator-01",
        event_type="integrity_test",
        detail="Original append-only audit detail.",
    )
    metric_payload = json.loads(metric.model_dump_json())
    audit_payload = json.loads(audit.model_dump_json())
    legacy_metric_id = sha256(
        json.dumps(
            {"record_type": "metric", "payload": metric_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    legacy_audit_id = sha256(
        json.dumps(
            {"record_type": "audit", "payload": audit_payload},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    with store._engine.begin() as connection:
        connection.execute(
            text("UPDATE telemetry_records SET id=:id"), {"id": legacy_metric_id}
        )
        connection.execute(
            text("UPDATE audit_events SET id=:id"), {"id": legacy_audit_id}
        )
    assert store.list_metrics()[0] == metric
    assert store.list_audit("incident-integrity")[0] == audit

    with store._engine.begin() as connection:
        connection.execute(text("UPDATE telemetry_records SET metric='tampered_metric'"))
        connection.execute(
            text("UPDATE audit_events SET detail='Tampered audit detail.'")
        )

    with pytest.raises(ValueError, match="metric metadata.*id.*metric"):
        store.list_metrics()
    with pytest.raises(ValueError, match="audit event metadata.*id"):
        store.list_audit("incident-integrity")
    store.dispose()


def test_app_impact_requires_a_sustained_breach(project_root: Path) -> None:
    service = DemoService(root=project_root)
    baseline_probe = service.app_probe_records()[-1]
    sustained_breach = [
        baseline_probe.model_copy(
            update={
                "observed_at": baseline_probe.observed_at + timedelta(seconds=offset),
                "packet_loss_ratio": 0.06,
                "jitter_ms": 35.0,
            }
        )
        for offset in (5, 10, 20)
    ]

    service.ingest_app_probes(sustained_breach)

    assert service._app_impact_started() is True
    assert service.incident().state == "escalated"
    assert service.incident().severity == "high"
    assert "already present" in service.incident().summary


def test_app_impact_ignores_a_sustained_breach_on_the_backup_path(project_root: Path) -> None:
    service = DemoService(root=project_root)
    baseline_probe = service.app_probe_records()[-1]
    backup_breach = [
        baseline_probe.model_copy(
            update={
                "observed_at": baseline_probe.observed_at + timedelta(seconds=offset),
                "path_id": "path:branch-01-to-dc:backup",
                "packet_loss_ratio": 0.06,
                "jitter_ms": 35.0,
            }
        )
        for offset in (5, 10, 20)
    ]

    service.ingest_app_probes(backup_breach)

    assert service._app_impact_started() is False


def test_ingestion_rejects_nonphysical_metric_values(project_root: Path) -> None:
    service = DemoService(root=project_root)
    invalid_loss = service.metric_records()[0].model_copy(update={"value": -0.5})

    with pytest.raises(ValueError, match="physical policy bounds"):
        service.ingest_metrics([invalid_loss])


def test_ingestion_rejects_unknown_event_entities(project_root: Path) -> None:
    service = DemoService(root=project_root)

    with pytest.raises(ValueError, match="event entity is not in the current topology"):
        service.ingest_events(
            [
                EventRecord.model_validate(
                    {
                        "observed_at": "2026-08-20T09:00:00Z",
                        "source": "collector",
                        "entity_id": "device:unknown",
                        "event_type": "interface_down",
                        "severity": "error",
                        "message": "Unknown device event",
                        "topology_version": "lab-v1",
                        "config_version": "lab-config-v1",
                        "quality": {"source_healthy": True},
                    }
                )
            ]
        )


@pytest.mark.parametrize("outcome", ["failed", "partially_resolved", "uncertain"])
def test_failed_closure_escalates_instead_of_claiming_resolution(
    project_root: Path, outcome: str
) -> None:
    service = DemoService(root=project_root)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")

    service.close(
        service.incident_id,
        ClosureRequest.model_validate(
            {"actor_id": "operator-01", "outcome": outcome, "note": "Recovery is incomplete."}
        ),
    )

    assert service.incident().state == "escalated"


@pytest.mark.parametrize("persisted", [False, True])
def test_closure_review_is_independent_and_new_evidence_rearms_incident(
    project_root: Path, monkeypatch, persisted: bool
) -> None:
    store = PersistenceStore("sqlite://") if persisted else None
    if store is not None:
        store.create_schema()
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(actor_id="operator-01", outcome="resolved", note="Recovery validated."),
    )
    with pytest.raises(ValueError, match="different actor"):
        service.review_closure(
            service.incident_id,
            ClosureReviewRequest(actor_id="operator-01", decision="approved", note="Self review."),
        )
    assert service.incident().state == "validating"

    service.review_closure(
        service.incident_id,
        ClosureReviewRequest(actor_id="reviewer-01", decision="approved", note="Evidence checked."),
    )
    if store is not None:
        service = DemoService(root=project_root, store=store)
    assert service.incident().state == "resolved"

    original_prediction = service.prediction()
    monkeypatch.setattr(
        service,
        "prediction",
        lambda: original_prediction.model_copy(update={"prediction_id": "pred-new-evidence"}),
    )
    assert service.incident().state == "predicted"
    with pytest.raises(PredictionWithheldError, match="evidence changed"):
        service.close(
            service.incident_id,
            ClosureRequest(actor_id="operator-01", outcome="resolved", note="Old review."),
        )


def test_application_impact_overrides_a_previously_approved_closure(project_root: Path) -> None:
    service = DemoService(root=project_root)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(actor_id="operator-01", outcome="resolved", note="Recovery validated."),
    )
    service.review_closure(
        service.incident_id,
        ClosureReviewRequest(actor_id="reviewer-01", decision="approved", note="Evidence checked."),
    )
    assert service.incident().state == "resolved"
    baseline_probe = service.app_probe_records()[-1]
    service.ingest_app_probes(
        [
            baseline_probe.model_copy(
                update={
                    "observed_at": baseline_probe.observed_at + timedelta(seconds=offset),
                    "packet_loss_ratio": 0.06,
                    "jitter_ms": 35.0,
                }
            )
            for offset in (5, 10, 20)
        ]
    )
    incident = service.incident()
    assert incident.state == "escalated"
    assert incident.severity == "high"


def test_prediction_identity_changes_with_evidence_at_the_same_timestamp(
    project_root: Path, monkeypatch
) -> None:
    service = DemoService(root=project_root)
    original_prediction = service.prediction()
    assert service.prediction().prediction_id == original_prediction.prediction_id
    features = service.feature_window()
    metric_name = next(iter(features.metrics))
    changed_metric = features.metrics[metric_name].model_copy(update={"latest": 0.01})
    changed_features = features.model_copy(
        update={"metrics": {**features.metrics, metric_name: changed_metric}}
    )
    monkeypatch.setattr(service, "feature_window", lambda: changed_features)
    assert service.prediction().prediction_id != original_prediction.prediction_id


def test_api_key_mode_enforces_identity_and_roles(project_root: Path) -> None:
    settings = Settings(
        environment="test",
        data_mode="replay",
        database_url="sqlite://",
        auth_mode="api_key",
        api_keys_json=json.dumps(
            {
                "operator-token": {"actor_id": "operator-01", "role": "operator"},
                "viewer-token": {"actor_id": "viewer-01", "role": "viewer"},
            }
        ),
    )
    store = PersistenceStore(settings.database_url)
    store.create_schema()
    client = TestClient(
        create_app(DemoService(root=project_root, store=store), settings=settings, store=store)
    )

    unauthenticated = client.get("/api/incidents")
    viewer = client.get("/api/incidents", headers={"Authorization": "Bearer viewer-token"})
    mismatched_actor = client.post(
        f"/api/incidents/{DemoService.incident_id}/review",
        headers={"Authorization": "Bearer operator-token"},
        json={"actor_id": "someone-else", "note": "review"},
    )
    valid_review = client.post(
        f"/api/incidents/{DemoService.incident_id}/review",
        headers={"Authorization": "Bearer operator-token"},
        json={"actor_id": "operator-01", "note": "review"},
    )

    assert unauthenticated.status_code == 401
    assert viewer.status_code == 200
    assert mismatched_actor.status_code == 403
    assert valid_review.status_code == 200
    assert valid_review.json()["state"] == "reviewed"


def test_production_settings_fail_without_production_gates() -> None:
    with pytest.raises(ValidationError, match="PRAHARI_DATA_MODE"):
        Settings(environment="production")


def test_production_startup_requires_applied_database_migrations(tmp_path: Path) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text(
        "postgresql+psycopg://prahari_app:placeholder@database/prahari",
        encoding="utf-8",
    )
    api_keys_file = tmp_path / "api-keys.json"
    api_keys_file.write_text(
        json.dumps(
            {
                "operator-token-with-32-characters-001": {
                    "actor_id": "operator-01",
                    "role": "operator",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        environment="production",
        data_mode="live",
        database_url_file=database_url_file,
        auth_mode="api_key",
        api_keys_json_file=api_keys_file,
        model_approval_status="approved",
        model_artifact_path=tmp_path / "model.txt",
        model_manifest_path=tmp_path / "model.manifest.json",
        copilot_enabled=True,
        auto_create_schema=False,
    )

    with pytest.raises(RuntimeError, match="database migrations"):
        create_app(settings=settings, store=_UnmigratedStore())  # type: ignore[arg-type]


def test_production_rejects_an_injected_replay_service(project_root: Path, tmp_path: Path) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text(
        "postgresql+psycopg://prahari_app:placeholder@database/prahari",
        encoding="utf-8",
    )
    api_keys_file = tmp_path / "api-keys.json"
    api_keys_file.write_text(
        json.dumps(
            {
                "operator-token-with-32-characters-001": {
                    "actor_id": "operator-01",
                    "role": "operator",
                }
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        environment="production",
        data_mode="live",
        database_url_file=database_url_file,
        auth_mode="api_key",
        api_keys_json_file=api_keys_file,
        model_approval_status="approved",
        model_artifact_path=tmp_path / "model.txt",
        model_manifest_path=tmp_path / "model.manifest.json",
        copilot_enabled=True,
        auto_create_schema=False,
    )

    with pytest.raises(RuntimeError, match="runtime factory"):
        create_app(DemoService(root=project_root), settings=settings)


def test_liveness_ready_endpoint_rejects_empty_live_telemetry(project_root: Path) -> None:
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

    response = client.get("/readyz")

    assert response.status_code == 503
    assert "live telemetry is not ready" in response.json()["detail"]


def test_unready_live_telemetry_blocks_workflow_writes(project_root: Path) -> None:
    store = PersistenceStore("sqlite://")
    store.create_schema()
    service = PrahariService(root=project_root, data_mode="live", store=store)

    with pytest.raises(PredictionWithheldError, match="workflow is unavailable"):
        service.review(service.incident_id, "operator-01", "review")

    assert store.incident_state(service.incident_id).reviewed_by is None
    assert store.list_audit(service.incident_id) == []


def test_live_readiness_requires_a_fresh_valid_application_probe(project_root: Path) -> None:
    service = PrahariService(root=project_root, data_mode="live")
    fixture_service = DemoService(root=project_root)
    fixture_metrics = fixture_service.metric_records()
    fixture_latest = max(record.observed_at for record in fixture_metrics)
    reference_time = datetime.now(UTC)
    current_metrics = [
        record.model_copy(
            update={"observed_at": reference_time - (fixture_latest - record.observed_at)}
        )
        for record in fixture_metrics
    ]

    service.ingest_metrics(current_metrics)
    is_ready, reason = service.readiness()

    assert is_ready is False
    assert "application probe" in reason
    with pytest.raises(PredictionWithheldError, match="application probe"):
        service.list_incidents()


def test_alembic_migration_marks_a_database_current(project_root: Path, tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    alembic_config = Config(str(project_root / "alembic.ini"))
    alembic_config.set_main_option("sqlalchemy.url", database_url)

    command.upgrade(alembic_config, "head")

    assert PersistenceStore(database_url).schema_is_current() is True
