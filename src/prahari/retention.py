"""Explicit maintenance policy; operational evidence is never pruned by the API."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text

from prahari.persistence import (
    AppProbeRow,
    EventRow,
    IncidentStateRow,
    PersistenceStore,
    TelemetryRow,
    _new_audit_event,
)

RETENTION_TABLES = (TelemetryRow, AppProbeRow, EventRow)


def _cutoff(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retention cutoff requires a UTC offset")
    value = value.astimezone(UTC)
    if value > datetime.now(UTC) - timedelta(days=1):
        raise ValueError("retention must preserve at least the most recent 24 hours")
    return value


def _eligible(table, cutoff):
    return (table.run_id.is_(None), table.observed_at < cutoff, table.ingested_at < cutoff)


def preview_retention(store: PersistenceStore, cutoff: datetime) -> dict:
    cutoff = _cutoff(cutoff)
    with store._session_factory() as session:
        active = (
            session.scalar(
                select(IncidentStateRow.incident_id)
                .where(IncidentStateRow.active_action_id.is_not(None))
                .limit(1)
            )
            is not None
        )
        counts = {
            table.__tablename__: session.scalar(
                select(func.count()).select_from(table).where(*_eligible(table, cutoff))
            )
            for table in RETENTION_TABLES
        }
    return {
        "cutoff": cutoff.isoformat(),
        "unconfirmed_action_present": active,
        "eligible_rows": counts,
        "executed": False,
    }


def prune_retention_batch(
    store: PersistenceStore,
    cutoff: datetime,
    *,
    actor_id: str,
    limit: int = 1000,
) -> dict:
    cutoff = _cutoff(cutoff)
    if not 1 <= limit <= 10000 or not actor_id.strip() or len(actor_id) > 100:
        raise ValueError("retention requires an actor and a batch size of 1-10000")
    with store._session_factory.begin() as session:
        if store._engine.dialect.name == "postgresql":
            # Serialize the small bounded batch against incident claims/decisions.
            # This is a maintenance-owner operation, not a runtime app grant.
            session.execute(text("SET LOCAL lock_timeout = '5s'"))
            session.execute(text("LOCK TABLE incident_states IN SHARE ROW EXCLUSIVE MODE"))
        else:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        if (
            session.scalar(
                select(IncidentStateRow.incident_id)
                .where(IncidentStateRow.active_action_id.is_not(None))
                .limit(1)
            )
            is not None
        ):
            raise ValueError("retention blocked: an unconfirmed action must retain its evidence")
        counts = {}
        deleted_id_sha256: dict[str, str | None] = {}
        remaining = limit
        for table in RETENTION_TABLES:
            ids = (
                list(
                    session.scalars(
                        select(table.id)
                        .where(*_eligible(table, cutoff))
                        .order_by(table.observed_at, table.id)
                        .limit(remaining)
                    )
                )
                if remaining
                else []
            )
            deleted_ids = (
                sorted(
                    session.scalars(
                        delete(table).where(table.id.in_(ids)).returning(table.id)
                    ).all()
                )
                if ids
                else []
            )
            count = len(deleted_ids)
            counts[table.__tablename__] = count
            deleted_id_sha256[table.__tablename__] = (
                hashlib.sha256(
                    json.dumps(deleted_ids, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                if deleted_ids
                else None
            )
            remaining -= count
        if any(counts.values()):
            store._add_audit_event(
                session,
                None,
                _new_audit_event(
                    actor_id,
                    "telemetry_retention_pruned",
                    f"Cutoff {cutoff.isoformat()}; bounded batch {limit}; removed {counts}. "
                    f"Deleted-ID SHA-256 {deleted_id_sha256}. "
                    "Training runs, workflow, action, case and audit records preserved.",
                ),
            )
    return {
        "cutoff": cutoff.isoformat(),
        "deleted_rows": counts,
        "deleted_id_sha256": deleted_id_sha256,
        "executed": True,
    }
