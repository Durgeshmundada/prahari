"""Real PostgreSQL checks; each test owns a random schema, never existing tables."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from alembic import command
from prahari.contracts import ActionReconciliationRequest, ClosureRequest, ClosureReviewRequest
from prahari.persistence import Base, BrowserSessionRow, PersistenceStore
from prahari.services.demo import DemoService


def test_postgres_concurrent_action_claim_has_one_winner(postgres_store, project_root):
    store, _ = postgres_store
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    prediction_id = service.prediction().prediction_id
    barrier = Barrier(2)

    def claim(kind):
        barrier.wait(timeout=10)
        try:
            service._begin_operation(
                kind, "operator-01", "Concurrent dispatch claim.", prediction_id
            )
            return "claimed"
        except ValueError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(claim, ["apply", "rollback"]))
    assert sorted(results) == ["blocked", "claimed"]
    assert len(store.list_actions(service.incident_id)) == 1
    assert store.has_unconfirmed_action(service.incident_id)
    counts = store.verify_evidence_integrity(batch_size=1)
    assert counts["action_operations"] == 1
    assert counts["incident_states"] == 1
    with pytest.raises(ValueError, match="unconfirmed"):
        store.review_with_audit(
            service.incident_id, "operator-02", "Cannot overwrite active claim.", prediction_id
        )


def test_postgres_concurrent_action_reconciliation_has_one_winner(postgres_store, project_root):
    store, _ = postgres_store
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    operation = service._begin_operation(
        "apply", "operator-01", "Worker stopped before outcome.", service.prediction().prediction_id
    )
    barrier = Barrier(2)

    def reconcile(actor):
        request = ActionReconciliationRequest(
            actor_id=actor,
            note="Dispatch stopped and route verified independently in this test.",
            observed_route="primary",
            evidence_reference="test-only-route-evidence",
            evidence_sha256="e" * 64,
            dispatch_quiesced=True,
        )
        barrier.wait(timeout=10)
        try:
            store.reconcile_action(operation.id, request)
            return "reconciled"
        except ValueError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(reconcile, ["reviewer-01", "reviewer-02"]))
    assert sorted(results) == ["blocked", "reconciled"]
    assert store.has_unconfirmed_action(service.incident_id) is False
    assert store.list_actions(service.incident_id)[0].reconciliation is not None
    counts = store.verify_evidence_integrity(batch_size=1)
    assert counts["action_operations"] == 1
    assert counts["incident_states"] == 1
    assert (
        sum(
            event.event_type == "action_reconciled"
            for event in store.list_audit(service.incident_id)
        )
        == 1
    )


@pytest.fixture
def postgres_store(monkeypatch):
    url = os.environ.get("PRAHARI_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set PRAHARI_TEST_POSTGRES_URL to run real PostgreSQL integration checks")
    assert make_url(url).get_backend_name() == "postgresql"
    schema = "prahari_test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(url).update_query_dict({"options": "-csearch_path=" + schema})
    database_url = scoped_url.render_as_string(hide_password=False)
    store = PersistenceStore(database_url)
    try:
        monkeypatch.delenv("PRAHARI_DATABASE_URL_FILE", raising=False)
        monkeypatch.setenv("PRAHARI_DATABASE_URL", database_url)
        command.upgrade(Config("alembic.ini"), "head")
        yield store, database_url
    finally:
        store.dispose()
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def test_postgres_migrations_and_concurrent_login_counter(postgres_store):
    store, url = postgres_store
    assert set(Base.metadata.tables) <= set(inspect(store._engine).get_table_names())
    assert store.schema_is_current()

    def increment(_):
        return store.record_login_attempt("global", window_seconds=300)

    with ThreadPoolExecutor(max_workers=8) as workers:
        counts = list(workers.map(increment, range(24)))
    assert sorted(counts) == list(range(1, 25))
    restarted = PersistenceStore(url)
    try:
        assert restarted.record_login_attempt("global", window_seconds=300) == 25
    finally:
        restarted.dispose()


def test_postgres_retention_preserves_pending_action_evidence(postgres_store, project_root):
    from prahari.persistence import TelemetryRow
    from prahari.retention import preview_retention, prune_retention_batch

    store, _ = postgres_store
    old = datetime.now(UTC) - timedelta(days=60)
    with store._session_factory.begin() as session:
        session.add(
            TelemetryRow(
                id="test-old-metric",
                entity_id="test-entity",
                metric="loss_ratio",
                observed_at=old,
                ingested_at=old,
                payload={"test-only": True},
            )
        )
    cutoff = datetime.now(UTC) - timedelta(days=30)
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    operation = service._begin_operation(
        "apply", "operator-01", "Pending test intent.", service.prediction().prediction_id
    )
    with pytest.raises(ValueError, match="unconfirmed action"):
        prune_retention_batch(store, cutoff, actor_id="maintenance-test")
    assert preview_retention(store, cutoff)["eligible_rows"]["telemetry_records"] == 1
    service.reconcile_action(
        operation.id,
        ActionReconciliationRequest(
            actor_id="reviewer-01",
            note="Dispatch stopped and route inspected independently in test.",
            observed_route="primary",
            evidence_reference="test-evidence",
            evidence_sha256="e" * 64,
            dispatch_quiesced=True,
        ),
    )
    result = prune_retention_batch(store, cutoff, actor_id="maintenance-test")
    assert result["deleted_rows"]["telemetry_records"] == 1
    assert store.list_actions(service.incident_id)[0].reconciliation is not None


def test_postgres_concurrent_case_decision_has_one_winner(postgres_store, project_root):
    store, _ = postgres_store
    service = DemoService(root=project_root, store=store)
    service.review(service.incident_id, "operator-01", "Reviewed current evidence.")
    service.close(
        service.incident_id,
        ClosureRequest(actor_id="operator-01", outcome="resolved", note="Recovery was measured."),
    )
    case = service.list_closure_cases()[0]
    barrier = Barrier(2)

    def decide(decision):
        barrier.wait(timeout=10)
        try:
            store.decide_closure_case(
                case.id,
                ClosureReviewRequest(
                    actor_id="reviewer-01", decision=decision, note="Independent checks."
                ),
            )
            return decision
        except ValueError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(decide, ["approved", "rejected"]))
    assert results.count("conflict") == 1
    winner = next(result for result in results if result != "conflict")
    assert store.closure_case(case.id).review.decision == winner
    assert store.incident_state(service.incident_id).closure_review.decision == winner
    decisions = [
        event
        for event in store.list_audit(service.incident_id)
        if event.event_type.startswith("closure_review_")
    ]
    assert len(decisions) == 1


def test_postgres_session_cap_and_revocation_survive_new_store(postgres_store):
    store, url = postgres_store
    bucket = sha256(b"operator").hexdigest()[:2]
    store.record_login_attempt(f"user-{bucket}", window_seconds=300)

    def create(index):
        now = datetime.now(UTC)
        row = BrowserSessionRow(
            token_hash=sha256(str(index).encode()).hexdigest(),
            username="operator",
            actor_id="operator-01",
            credential_fingerprint="a" * 64,
            csrf_token="b" * 43,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(hours=1),
        )
        store.create_browser_session(row, old_token_hash=None)

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(create, range(12)))
    with store._session_factory() as session:
        tokens = list(session.scalars(select(BrowserSessionRow.token_hash)))
    assert len(tokens) == 5
    store.revoke_browser_session(tokens[0], "operator-01")
    restarted = PersistenceStore(url)
    try:
        assert restarted.browser_session(tokens[0], idle_seconds=1800) is None
        assert restarted.browser_session(tokens[1], idle_seconds=1800) is not None
    finally:
        restarted.dispose()
