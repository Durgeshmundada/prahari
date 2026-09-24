from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from sqlalchemy import select

from prahari.persistence import (
    AppProbeRow,
    AuditEventRow,
    EventRow,
    IncidentStateRow,
    PersistenceStore,
    TelemetryRow,
)
from prahari.retention import preview_retention, prune_retention_batch


@pytest.fixture
def store():
    store = PersistenceStore("sqlite:///:memory:")
    store.create_schema()
    now = datetime.now(UTC)
    with store._session_factory.begin() as session:
        for table in (TelemetryRow, AppProbeRow, EventRow):
            for number, (run, observed, ingested) in enumerate(
                [
                    (None, now - timedelta(days=60), now - timedelta(days=60)),
                    ("training-run", now - timedelta(days=60), now - timedelta(days=60)),
                    (None, now - timedelta(days=60), now),
                    (None, now, now),
                ]
            ):
                fields = dict(
                    id=f"{table.__tablename__}-{number}",
                    run_id=run,
                    entity_id="test-entity",
                    observed_at=observed,
                    ingested_at=ingested,
                    payload={"test-only": True},
                )
                if table is TelemetryRow:
                    fields["metric"] = "loss_ratio"
                if table is EventRow:
                    fields["event_type"] = "test-event"
                session.add(table(**fields))
    yield store
    store.dispose()


def test_preview_never_deletes_and_execution_preserves_training_and_recent_ingestion(store):
    cutoff = datetime.now(UTC) - timedelta(days=30)
    preview = preview_retention(store, cutoff)
    assert list(preview["eligible_rows"].values()) == [1, 1, 1]
    assert preview["executed"] is False
    result = prune_retention_batch(store, cutoff, actor_id="maintenance-test", limit=2)
    assert sum(result["deleted_rows"].values()) == 2
    assert all(
        digest is None or len(digest) == 64
        for digest in result["deleted_id_sha256"].values()
    )
    assert result["deleted_id_sha256"]["telemetry_records"] == sha256(
        b'["telemetry_records-0"]'
    ).hexdigest()
    result = prune_retention_batch(store, cutoff, actor_id="maintenance-test", limit=2)
    assert sum(result["deleted_rows"].values()) == 1
    with store._session_factory() as session:
        for table in (TelemetryRow, AppProbeRow, EventRow):
            ids = list(session.scalars(select(table.id)))
            assert len(ids) == 3 and all(not value.endswith("-0") for value in ids)
        assert len(list(session.scalars(select(AuditEventRow)))) == 2


def test_unconfirmed_claim_blocks_pruning(store):
    with store._session_factory.begin() as session:
        session.add(
            IncidentStateRow(
                incident_id="test-incident",
                active_action_id="pending-test",
                updated_at=datetime.now(UTC),
            )
        )
    cutoff = datetime.now(UTC) - timedelta(days=30)
    assert preview_retention(store, cutoff)["unconfirmed_action_present"] is True
    with pytest.raises(ValueError, match="unconfirmed action"):
        prune_retention_batch(store, cutoff, actor_id="maintenance-test")
    assert sum(preview_retention(store, cutoff)["eligible_rows"].values()) == 3


def test_audit_failure_rolls_back_deletion(store, monkeypatch):
    monkeypatch.setattr(
        store,
        "_add_audit_event",
        lambda *args: (_ for _ in ()).throw(RuntimeError("test storage failure")),
    )
    cutoff = datetime.now(UTC) - timedelta(days=30)
    with pytest.raises(RuntimeError):
        prune_retention_batch(store, cutoff, actor_id="maintenance-test")
    assert sum(preview_retention(store, cutoff)["eligible_rows"].values()) == 3


def test_cutoff_cannot_remove_current_prediction_window(store):
    with pytest.raises(ValueError, match="most recent 24 hours"):
        prune_retention_batch(store, datetime.now(UTC), actor_id="maintenance-test")
    with pytest.raises(ValueError, match="UTC offset"):
        preview_retention(store, datetime.now())
