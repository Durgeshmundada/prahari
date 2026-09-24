"""Durable local persistence for telemetry, workflow state, and audit evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    JSON,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    case,
    create_engine,
    delete,
    desc,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

from prahari.config import project_root
from prahari.contracts import (
    ActionOperation,
    ActionReconciliationRequest,
    AppProbeRecord,
    AuditEvent,
    ClosureCase,
    ClosureRequest,
    ClosureReviewRequest,
    EventRecord,
    MetricRecord,
)


class Base(DeclarativeBase):
    """SQLAlchemy metadata root."""


class TelemetryRow(Base):
    __tablename__ = "telemetry_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    entity_id: Mapped[str] = mapped_column(String(255), index=True)
    metric: Mapped[str] = mapped_column(String(100), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AppProbeRow(Base):
    __tablename__ = "app_probe_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    entity_id: Mapped[str] = mapped_column(String(255), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EventRow(Base):
    __tablename__ = "event_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    entity_id: Mapped[str] = mapped_column(String(255), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class IncidentStateRow(Base):
    __tablename__ = "incident_states"

    incident_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    reviewed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    closure_payload: Mapped[dict[str, object] | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    active_action_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ActionOperationRow(Base):
    __tablename__ = "action_operations"
    __table_args__ = (
        UniqueConstraint(
            "incident_id", "playbook_id", "prediction_id", "kind", name="uq_action_prediction_kind"
        ),
    )

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(100), index=True)
    playbook_id: Mapped[str] = mapped_column(String(100))
    prediction_id: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    actor_id: Mapped[str] = mapped_column(String(100), index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    detail: Mapped[str] = mapped_column(Text)


class ClosureCaseRow(Base):
    __tablename__ = "closure_cases"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(100), index=True)
    prediction_id: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    incident_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    network_profile_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSON)


class BrowserSessionRow(Base):
    __tablename__ = "browser_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(100), index=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    credential_fingerprint: Mapped[str] = mapped_column(String(64))
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginWindowRow(Base):
    __tablename__ = "login_windows"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer)


@dataclass(frozen=True)
class StoredIncidentState:
    reviewed_by: str | None
    review_note: str | None
    reviewed_prediction_id: str | None
    closure: ClosureRequest | None
    closure_prediction_id: str | None
    closure_review: ClosureReviewRequest | None
    closure_case_id: str | None = None


class PersistenceStore:
    """Small SQLAlchemy repository that works with SQLite and PostgreSQL URLs."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._ensure_sqlite_parent(database_url)
        engine_options: dict[str, object] = {"future": True, "pool_pre_ping": True}
        if database_url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool
        self._engine = create_engine(database_url, **engine_options)
        self._session_factory = sessionmaker(
            bind=self._engine,
            class_=Session,
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        inspector = inspect(self._engine)
        if "incident_states" in inspector.get_table_names() and "active_action_id" not in {
            column["name"] for column in inspector.get_columns("incident_states")
        }:
            raise RuntimeError(
                "existing database requires migration 0004_action_operations; "
                "preserve a backup and run alembic upgrade head before startup"
            )
        if "closure_cases" in inspector.get_table_names() and "outcome" not in {
            column["name"] for column in inspector.get_columns("closure_cases")
        }:
            raise RuntimeError(
                "existing database requires migration 0005_case_search; "
                "preserve a backup and run alembic upgrade head before startup"
            )
        Base.metadata.create_all(self._engine)

    def dispose(self) -> None:
        self._engine.dispose()

    def healthcheck(self) -> bool:
        with self._engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    def verify_evidence_integrity(self, *, batch_size: int = 1000) -> dict[str, int]:
        """Validate retained evidence rows and their workflow relationships."""

        if not 1 <= batch_size <= 10_000:
            raise ValueError("integrity verification batch size must be 1-10000")
        tables = (
            (TelemetryRow, _metric_record_from_row),
            (AppProbeRow, _app_probe_record_from_row),
            (EventRow, _event_record_from_row),
            (AuditEventRow, _audit_event_from_row),
            (ClosureCaseRow, _closure_case_from_row),
            (ActionOperationRow, _action_operation_from_row),
        )
        counts: dict[str, int] = {}
        with self._session_factory.begin() as session:
            if self._engine.dialect.name == "postgresql":
                session.execute(
                    text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                )
            for table, validator in tables:
                count = 0
                rows = session.scalars(select(table).order_by(table.id)).yield_per(batch_size)
                for row in rows:
                    validator(row)
                    count += 1
                counts[table.__tablename__] = count
            counts[IncidentStateRow.__tablename__] = _verify_incident_relationships(
                session,
                batch_size=batch_size,
            )
        return counts

    def schema_is_current(self) -> bool:
        """Verify all expected tables exist and the database is at Alembic head."""

        required_tables = {
            "alembic_version",
            "telemetry_records",
            "app_probe_records",
            "event_records",
            "incident_states",
            "audit_events",
            "closure_cases",
            "browser_sessions",
            "login_windows",
            "action_operations",
        }
        if not required_tables.issubset(set(inspect(self._engine).get_table_names())):
            return False
        with self._engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if not isinstance(revision, str):
            return False
        alembic_config = Config(str(project_root() / "alembic.ini"))
        expected_revision = ScriptDirectory.from_config(alembic_config).get_current_head()
        return revision == expected_revision

    def save_metrics(self, records: list[MetricRecord], *, run_id: str | None = None) -> int:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            for record in records:
                payload = _json_payload(record)
                session.merge(
                    TelemetryRow(
                        id=_record_id("metric", payload, run_id=run_id),
                        run_id=run_id,
                        entity_id=record.entity_id,
                        metric=record.metric,
                        observed_at=record.observed_at,
                        payload=payload,
                        ingested_at=now,
                    )
                )
        return len(records)

    def replace_replay_fixture(
        self,
        metrics: list[MetricRecord],
        app_probes: list[AppProbeRecord],
        *,
        run_id: str,
    ) -> None:
        """Atomically refresh one named replay dataset without touching live evidence.

        Replay fixtures may change while retaining their stable run ID. Removing
        the previous rows for that exact replay run prevents old and new fixture
        revisions from creating duplicate timestamps in prediction windows.
        Production/live services never call this replacement path.
        """

        if not run_id.strip():
            raise ValueError("replay fixture run_id cannot be empty")
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            session.execute(delete(TelemetryRow).where(TelemetryRow.run_id == run_id))
            session.execute(delete(AppProbeRow).where(AppProbeRow.run_id == run_id))
            for record in metrics:
                payload = _json_payload(record)
                session.add(
                    TelemetryRow(
                        id=_record_id("metric", payload, run_id=run_id),
                        run_id=run_id,
                        entity_id=record.entity_id,
                        metric=record.metric,
                        observed_at=record.observed_at,
                        payload=payload,
                        ingested_at=now,
                    )
                )
            for record in app_probes:
                payload = _json_payload(record)
                session.add(
                    AppProbeRow(
                        id=_record_id("app_probe", payload, run_id=run_id),
                        run_id=run_id,
                        entity_id=record.entity_id,
                        observed_at=record.observed_at,
                        payload=payload,
                        ingested_at=now,
                    )
                )

    def save_app_probes(self, records: list[AppProbeRecord], *, run_id: str | None = None) -> int:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            for record in records:
                payload = _json_payload(record)
                session.merge(
                    AppProbeRow(
                        id=_record_id("app_probe", payload, run_id=run_id),
                        run_id=run_id,
                        entity_id=record.entity_id,
                        observed_at=record.observed_at,
                        payload=payload,
                        ingested_at=now,
                    )
                )
        return len(records)

    def save_events(self, records: list[EventRecord], *, run_id: str | None = None) -> int:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            for record in records:
                payload = _json_payload(record)
                session.merge(
                    EventRow(
                        id=_record_id("event", payload, run_id=run_id),
                        run_id=run_id,
                        entity_id=record.entity_id,
                        event_type=record.event_type,
                        observed_at=record.observed_at,
                        payload=payload,
                        ingested_at=now,
                    )
                )
        return len(records)

    def list_metrics(
        self,
        *,
        entity_id: str | None = None,
        metric: str | None = None,
        limit: int = 1_000,
    ) -> list[MetricRecord]:
        statement = select(TelemetryRow).order_by(desc(TelemetryRow.observed_at)).limit(limit)
        if entity_id:
            statement = statement.where(TelemetryRow.entity_id == entity_id)
        if metric:
            statement = statement.where(TelemetryRow.metric == metric)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
        return [_metric_record_from_row(row) for row in reversed(rows)]

    def list_app_probes(
        self,
        *,
        entity_id: str | None = None,
        before: datetime | None = None,
        limit: int = 1_000,
    ) -> list[AppProbeRecord]:
        statement = select(AppProbeRow)
        if entity_id:
            statement = statement.where(AppProbeRow.entity_id == entity_id)
        if before:
            statement = statement.where(AppProbeRow.observed_at < before)
        statement = statement.order_by(desc(AppProbeRow.observed_at)).limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
        return [_app_probe_record_from_row(row) for row in reversed(rows)]

    def list_events(
        self,
        *,
        entity_id: str | None = None,
        before: datetime | None = None,
        limit: int = 1_000,
    ) -> list[EventRecord]:
        statement = select(EventRow)
        if entity_id:
            statement = statement.where(EventRow.entity_id == entity_id)
        if before:
            statement = statement.where(EventRow.observed_at < before)
        statement = statement.order_by(desc(EventRow.observed_at)).limit(limit)
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
        return [_event_record_from_row(row) for row in reversed(rows)]

    def has_telemetry(self) -> bool:
        with self._session_factory() as session:
            return session.scalar(select(TelemetryRow.id).limit(1)) is not None

    def incident_state(self, incident_id: str) -> StoredIncidentState:
        with self._session_factory() as session:
            row = session.get(IncidentStateRow, incident_id)
            if row is None:
                return StoredIncidentState(None, None, None, None, None, None)
            review_note, reviewed_prediction_id = _decode_review_state(row.review_note)
            closure, closure_prediction_id, closure_review = _decode_closure_state(
                row.closure_payload
            )
            return StoredIncidentState(
                reviewed_by=row.reviewed_by,
                review_note=review_note,
                reviewed_prediction_id=reviewed_prediction_id,
                closure=closure,
                closure_prediction_id=closure_prediction_id,
                closure_review=closure_review,
                closure_case_id=(row.closure_payload or {}).get("case_id"),
            )

    def review_with_audit(
        self,
        incident_id: str,
        actor_id: str,
        note: str,
        prediction_id: str,
    ) -> AuditEvent:
        """Persist review state and its audit evidence in one transaction."""

        event = _new_audit_event(
            actor_id, "incident_reviewed", f"Prediction {prediction_id}. {note}"
        )
        with self._session_factory.begin() as session:
            row = session.get(IncidentStateRow, incident_id)
            if row is None:
                row = IncidentStateRow(
                    incident_id=incident_id,
                    reviewed_by=actor_id,
                    review_note=_encode_review_state(note, prediction_id),
                    closure_payload=None,
                    updated_at=datetime.now(UTC),
                )
                session.add(row)
            else:
                changed = session.execute(
                    update(IncidentStateRow)
                    .where(
                        IncidentStateRow.incident_id == incident_id,
                        IncidentStateRow.active_action_id.is_(None),
                    )
                    .values(
                        reviewed_by=actor_id,
                        review_note=_encode_review_state(note, prediction_id),
                        closure_payload=None,
                        updated_at=datetime.now(UTC),
                    )
                )
                if changed.rowcount != 1:
                    raise ValueError("unconfirmed action must be reconciled before a new review")
            self._add_audit_event(session, incident_id, event)
        return event

    def close_with_audit(
        self,
        incident_id: str,
        request: ClosureRequest,
        prediction_id: str,
        *,
        case: ClosureCase | None = None,
    ) -> AuditEvent:
        """Persist closure state and its audit evidence in one transaction."""

        event = _new_audit_event(
            request.actor_id,
            "closure_submitted",
            f"Prediction {prediction_id}. {request.outcome}: {request.note}",
        )
        with self._session_factory.begin() as session:
            if case is not None:
                session.add(
                    ClosureCaseRow(
                        id=case.id,
                        incident_id=case.incident_id,
                        prediction_id=case.prediction_id,
                        created_at=case.created_at,
                        status="pending",
                        outcome=case.submission.outcome,
                        incident_type=case.incident.prediction.likely_incident_type,
                        entity_id=case.incident.prediction.entity_id,
                        network_profile_sha256=case.incident.prediction.network_profile_sha256,
                        payload=_json_payload(case),
                    )
                )
            row = session.get(IncidentStateRow, incident_id)
            if row is None:
                row = IncidentStateRow(
                    incident_id=incident_id,
                    reviewed_by=None,
                    review_note=None,
                    closure_payload=_encode_closure_state(
                        request, prediction_id, case_id=case.id if case else None
                    ),
                    updated_at=datetime.now(UTC),
                )
                session.add(row)
            else:
                conditions = [
                    IncidentStateRow.incident_id == incident_id,
                    IncidentStateRow.active_action_id.is_(None),
                ]
                if case is not None:
                    if (
                        _decode_review_state(row.review_note)[1] != prediction_id
                        or row.reviewed_by != request.actor_id
                    ):
                        raise ValueError("closure requires the current operator review")
                    conditions.extend(
                        [
                            IncidentStateRow.reviewed_by == request.actor_id,
                            IncidentStateRow.review_note == row.review_note,
                        ]
                    )
                changed = session.execute(
                    update(IncidentStateRow)
                    .where(*conditions)
                    .values(
                        closure_payload=_encode_closure_state(
                            request, prediction_id, case_id=case.id if case else None
                        ),
                        updated_at=datetime.now(UTC),
                    )
                )
                if changed.rowcount != 1:
                    raise ValueError("review changed or an unconfirmed action blocks closure")
            self._add_audit_event(session, incident_id, event)
        return event

    def review_closure_with_audit(
        self,
        incident_id: str,
        request: ClosureReviewRequest,
        prediction_id: str,
    ) -> AuditEvent:
        event = _new_audit_event(
            request.actor_id,
            f"closure_review_{request.decision}",
            f"Prediction {prediction_id}. {request.note}",
        )
        with self._session_factory.begin() as session:
            row = session.get(IncidentStateRow, incident_id)
            if row is None:
                raise ValueError("closure case does not exist")
            closure, closure_prediction_id, _ = _decode_closure_state(row.closure_payload)
            if closure is None or closure_prediction_id != prediction_id:
                raise ValueError("closure case does not match the current prediction evidence")
            if request.actor_id == closure.actor_id:
                raise ValueError("closure must be reviewed by a different actor than its submitter")
            row.closure_payload = _encode_closure_state(
                closure,
                prediction_id,
                review=request,
            )
            row.updated_at = datetime.now(UTC)
            self._add_audit_event(session, incident_id, event)
        return event

    def closure_case(self, case_id: str) -> ClosureCase:
        with self._session_factory() as session:
            row = session.get(ClosureCaseRow, case_id)
            if row is None:
                raise KeyError(f"unknown closure case {case_id!r}")
            return _closure_case_from_row(row)

    def record_login_attempt(self, key: str, *, window_seconds: int) -> int:
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=window_seconds)
        insert = sqlite_insert if self._engine.dialect.name == "sqlite" else postgres_insert
        statement = insert(LoginWindowRow).values(key=key, window_start=now, attempts=1)
        statement = statement.on_conflict_do_update(
            index_elements=[LoginWindowRow.key],
            set_={
                "window_start": case(
                    (LoginWindowRow.window_start <= cutoff, now), else_=LoginWindowRow.window_start
                ),
                "attempts": case(
                    (LoginWindowRow.window_start <= cutoff, 1), else_=LoginWindowRow.attempts + 1
                ),
            },
        ).returning(LoginWindowRow.attempts)
        with self._session_factory.begin() as session:
            return session.execute(statement).scalar_one()

    def create_browser_session(self, row: BrowserSessionRow, *, old_token_hash: str | None) -> None:
        with self._session_factory.begin() as session:
            bucket = hashlib.sha256(row.username.encode()).hexdigest()[:2]
            session.get(LoginWindowRow, f"user-{bucket}", with_for_update=True)
            session.execute(
                delete(BrowserSessionRow).where(BrowserSessionRow.expires_at <= row.created_at)
            )
            if old_token_hash is not None:
                session.execute(
                    delete(BrowserSessionRow).where(BrowserSessionRow.token_hash == old_token_hash)
                )
            older = session.scalars(
                select(BrowserSessionRow.token_hash)
                .where(BrowserSessionRow.username == row.username)
                .order_by(BrowserSessionRow.created_at.desc())
                .offset(4)
            ).all()
            if older:
                session.execute(
                    delete(BrowserSessionRow).where(BrowserSessionRow.token_hash.in_(older))
                )
            session.add(row)
            self._add_audit_event(
                session,
                None,
                _new_audit_event(
                    row.actor_id, "browser_login", "Authenticated browser session created."
                ),
            )

    def browser_session(self, token_hash: str, *, idle_seconds: int) -> BrowserSessionRow | None:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            return session.execute(
                update(BrowserSessionRow)
                .where(
                    BrowserSessionRow.token_hash == token_hash,
                    BrowserSessionRow.expires_at > now,
                    BrowserSessionRow.last_seen_at > now - timedelta(seconds=idle_seconds),
                )
                .values(last_seen_at=now)
                .returning(BrowserSessionRow)
            ).scalar_one_or_none()

    def revoke_browser_session(self, token_hash: str, actor_id: str) -> None:
        with self._session_factory.begin() as session:
            session.execute(
                delete(BrowserSessionRow).where(BrowserSessionRow.token_hash == token_hash)
            )
            self._add_audit_event(
                session,
                None,
                _new_audit_event(actor_id, "browser_logout", "Browser session revoked."),
            )

    def list_closure_cases(
        self,
        *,
        status: str | None = None,
        outcome: str | None = None,
        incident_type: str | None = None,
        entity_id: str | None = None,
        network_profile_sha256: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ClosureCase]:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("case limit must be 1-200 and offset must be non-negative")
        if status not in {None, "pending", "approved", "rejected"}:
            raise ValueError("unknown closure case status")
        statement = select(ClosureCaseRow)
        if status is not None:
            statement = statement.where(ClosureCaseRow.status == status)
        if outcome is not None:
            statement = statement.where(ClosureCaseRow.outcome == outcome)
        if incident_type is not None:
            statement = statement.where(ClosureCaseRow.incident_type == incident_type)
        if entity_id is not None:
            statement = statement.where(ClosureCaseRow.entity_id == entity_id)
        if network_profile_sha256 is not None:
            statement = statement.where(
                ClosureCaseRow.network_profile_sha256 == network_profile_sha256
            )
        statement = (
            statement.order_by(ClosureCaseRow.created_at.desc(), ClosureCaseRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._session_factory() as session:
            return [_closure_case_from_row(row) for row in session.scalars(statement)]

    def decide_closure_case(self, case_id: str, request: ClosureReviewRequest) -> ClosureCase:
        with self._session_factory.begin() as session:
            row = session.get(ClosureCaseRow, case_id)
            if row is None:
                raise KeyError(f"unknown closure case {case_id!r}")
            case = _closure_case_from_row(row)
            if request.actor_id == case.submission.actor_id:
                raise ValueError("closure must be reviewed by a different actor than its submitter")
            if case.review is not None:
                raise ValueError("closure case already has a final review")
            reviewed = case.model_copy(update={"review": request, "reviewed_at": datetime.now(UTC)})
            changed = session.execute(
                update(ClosureCaseRow)
                .where(ClosureCaseRow.id == case_id, ClosureCaseRow.status == "pending")
                .values(status=request.decision, payload=_json_payload(reviewed))
            )
            if changed.rowcount != 1:
                raise ValueError("closure case was reviewed concurrently; reload the case")
            state_row = session.get(IncidentStateRow, case.incident_id, with_for_update=True)
            if (
                state_row is not None
                and (state_row.closure_payload or {}).get("case_id") == case_id
            ):
                state_row.closure_payload = _encode_closure_state(
                    case.submission, case.prediction_id, review=request, case_id=case_id
                )
                state_row.updated_at = datetime.now(UTC)
            event = _new_audit_event(
                request.actor_id,
                f"closure_review_{request.decision}",
                f"Case {case_id}; prediction {case.prediction_id}. {request.note}",
            )
            self._add_audit_event(session, case.incident_id, event)
            return reviewed

    def begin_action(self, operation: ActionOperation) -> AuditEvent:
        """Claim the incident and persist intent in the same authorization transaction."""
        if operation.prediction_snapshot.prediction_id != operation.prediction_id:
            raise ValueError("action snapshot does not match the requested prediction")
        event_type = "action_apply_requested" if operation.kind == "apply" else "rollback_requested"
        event = _new_audit_event(
            operation.actor_id,
            event_type,
            f"Operation {operation.id}; prediction {operation.prediction_id}. {operation.note}",
        )
        try:
            with self._session_factory.begin() as session:
                insert = sqlite_insert if self._engine.dialect.name == "sqlite" else postgres_insert
                session.execute(
                    insert(IncidentStateRow)
                    .values(
                        incident_id=operation.incident_id,
                        updated_at=operation.created_at,
                    )
                    .on_conflict_do_nothing(index_elements=[IncidentStateRow.incident_id])
                )
                row = session.get(IncidentStateRow, operation.incident_id)
                assert row is not None
                conditions = [
                    IncidentStateRow.incident_id == operation.incident_id,
                    IncidentStateRow.active_action_id.is_(None),
                ]
                if operation.kind == "apply":
                    if (
                        row.reviewed_by != operation.actor_id
                        or _decode_review_state(row.review_note)[1] != operation.prediction_id
                    ):
                        raise ValueError("action requires the current operator review")
                    cutoff = datetime.now(UTC) - timedelta(minutes=5)
                    recent = session.scalar(
                        select(AuditEventRow.id)
                        .where(
                            AuditEventRow.incident_id == operation.incident_id,
                            AuditEventRow.actor_id == operation.actor_id,
                            AuditEventRow.event_type == "incident_reviewed",
                            AuditEventRow.occurred_at >= cutoff,
                        )
                        .limit(1)
                    )
                    if recent is None:
                        raise ValueError("action requires a review within the last five minutes")
                    conditions.extend(
                        [
                            IncidentStateRow.reviewed_by == operation.actor_id,
                            IncidentStateRow.review_note == row.review_note,
                        ]
                    )
                claimed = session.execute(
                    update(IncidentStateRow)
                    .where(*conditions)
                    .values(
                        active_action_id=operation.id,
                        updated_at=operation.created_at,
                    )
                )
                if claimed.rowcount != 1:
                    raise ValueError(
                        "review changed or another unconfirmed action owns this incident"
                    )
                session.add(
                    ActionOperationRow(
                        id=operation.id,
                        incident_id=operation.incident_id,
                        playbook_id=operation.playbook_id,
                        prediction_id=operation.prediction_id,
                        kind=operation.kind,
                        status="pending",
                        created_at=operation.created_at,
                        payload=_json_payload(operation),
                    )
                )
                self._add_audit_event(session, operation.incident_id, event)
        except IntegrityError as error:
            raise ValueError(
                "an operation is already registered for this prediction and kind"
            ) from error
        return event

    def finish_action(self, operation: ActionOperation, event_type: str) -> AuditEvent:
        operation = ActionOperation.model_validate(operation.model_dump())
        event = _new_audit_event(
            operation.actor_id, event_type, f"Operation {operation.id}. {operation.message or ''}"
        )
        with self._session_factory.begin() as session:
            changed = session.execute(
                update(ActionOperationRow)
                .where(
                    ActionOperationRow.id == operation.id,
                    ActionOperationRow.status == "pending",
                )
                .values(status=operation.status, payload=_json_payload(operation))
            )
            if changed.rowcount != 1:
                raise ValueError("action was already finalized or reconciled")
            if operation.status == "finished":
                released = session.execute(
                    update(IncidentStateRow)
                    .where(
                        IncidentStateRow.incident_id == operation.incident_id,
                        IncidentStateRow.active_action_id == operation.id,
                    )
                    .values(active_action_id=None, updated_at=operation.updated_at)
                )
                if released.rowcount != 1:
                    raise ValueError("action no longer owns the incident; finalization aborted")
            self._add_audit_event(session, operation.incident_id, event)
        return event

    def has_unconfirmed_action(self, incident_id: str) -> bool:
        with self._session_factory() as session:
            return (
                session.scalar(
                    select(IncidentStateRow.active_action_id).where(
                        IncidentStateRow.incident_id == incident_id,
                    )
                )
                is not None
            )

    def list_actions(self, incident_id: str, *, limit: int = 100) -> list[ActionOperation]:
        if not 1 <= limit <= 1000:
            raise ValueError("action limit must be 1-1000")
        with self._session_factory() as session:
            rows = session.scalars(
                select(ActionOperationRow)
                .where(
                    ActionOperationRow.incident_id == incident_id,
                )
                .order_by(desc(ActionOperationRow.created_at), ActionOperationRow.id)
                .limit(limit)
            )
            return [_action_operation_from_row(row) for row in rows]

    def reconcile_action(
        self,
        operation_id: str,
        request: ActionReconciliationRequest,
    ) -> ActionOperation:
        with self._session_factory.begin() as session:
            row = session.get(ActionOperationRow, operation_id)
            if row is None:
                raise KeyError(f"unknown action operation {operation_id!r}")
            operation = _action_operation_from_row(row)
            if request.actor_id == operation.actor_id:
                raise ValueError("action reconciliation requires an independent reviewer")
            reconciled = ActionOperation.model_validate(
                operation.model_copy(
                    update={
                        "status": "finished",
                        "updated_at": datetime.now(UTC),
                        "reconciliation": request,
                        "observed_route": request.observed_route,
                    }
                ).model_dump()
            )
            changed = session.execute(
                update(ActionOperationRow)
                .where(
                    ActionOperationRow.id == operation_id,
                    ActionOperationRow.status.in_(["pending", "unknown"]),
                )
                .values(status="finished", payload=_json_payload(reconciled))
            )
            if changed.rowcount != 1:
                raise ValueError("action was already finalized or reconciled")
            released = session.execute(
                update(IncidentStateRow)
                .where(
                    IncidentStateRow.incident_id == operation.incident_id,
                    IncidentStateRow.active_action_id == operation_id,
                )
                .values(active_action_id=None, updated_at=reconciled.updated_at)
            )
            if released.rowcount != 1:
                raise ValueError("action no longer owns the incident; reconciliation aborted")
            self._add_audit_event(
                session,
                operation.incident_id,
                _new_audit_event(
                    request.actor_id,
                    "action_reconciled",
                    f"Operation {operation_id}; route {request.observed_route}; "
                    f"evidence {request.evidence_reference}; SHA-256 {request.evidence_sha256}; "
                    f"dispatch quiescence attested. {request.note}",
                ),
            )
            return reconciled

    def append_audit(
        self,
        *,
        incident_id: str | None,
        actor_id: str,
        event_type: str,
        detail: str,
    ) -> AuditEvent:
        event = _new_audit_event(actor_id, event_type, detail)
        with self._session_factory.begin() as session:
            self._add_audit_event(session, incident_id, event)
        return event

    def list_audit(
        self, incident_id: str, *, limit: int = 1000, offset: int = 0
    ) -> list[AuditEvent]:
        if not 1 <= limit <= 1000 or offset < 0:
            raise ValueError("audit limit must be 1-1000 and offset must be non-negative")
        statement = (
            select(AuditEventRow)
            .where(AuditEventRow.incident_id == incident_id)
            .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._session_factory() as session:
            rows = session.scalars(statement).all()
        return [_audit_event_from_row(row) for row in reversed(rows)]

    @staticmethod
    def _add_audit_event(
        session: Session,
        incident_id: str | None,
        event: AuditEvent,
    ) -> None:
        event_payload = _json_payload(event)
        session.add(
            AuditEventRow(
                id=_record_id("audit", event_payload, context=incident_id),
                incident_id=incident_id,
                occurred_at=event.occurred_at,
                actor_id=event.actor_id,
                event_type=event.event_type,
                detail=event.detail,
            )
        )

    @staticmethod
    def _ensure_sqlite_parent(database_url: str) -> None:
        prefix = "sqlite:///"
        if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
            return
        database_path = Path(database_url.removeprefix(prefix))
        database_path.parent.mkdir(parents=True, exist_ok=True)


def _json_payload(model: object) -> dict[str, object]:
    model_dump_json = getattr(model, "model_dump_json")
    return json.loads(model_dump_json())


def _encode_review_state(note: str, prediction_id: str) -> str:
    return json.dumps(
        {
            "schema_version": "incident-review-v1",
            "prediction_id": prediction_id,
            "note": note,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_review_state(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return value, None
    if (
        not isinstance(raw, dict)
        or raw.get("schema_version") != "incident-review-v1"
        or not isinstance(raw.get("note"), str)
        or not isinstance(raw.get("prediction_id"), str)
    ):
        return value, None
    return raw["note"], raw["prediction_id"]


def _encode_closure_state(
    closure: ClosureRequest,
    prediction_id: str,
    *,
    review: ClosureReviewRequest | None = None,
    case_id: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "closure-case-v1",
        "prediction_id": prediction_id,
        "submission": _json_payload(closure),
        "review": _json_payload(review) if review is not None else None,
        "case_id": case_id,
    }


def _decode_closure_state(
    value: dict[str, object] | None,
) -> tuple[ClosureRequest | None, str | None, ClosureReviewRequest | None]:
    if value is None:
        return None, None, None
    if value.get("schema_version") != "closure-case-v1":
        # Old rows remain readable but cannot authorize a current workflow action.
        return ClosureRequest.model_validate(value), None, None
    prediction_id = value.get("prediction_id")
    if not isinstance(prediction_id, str):
        raise ValueError("stored closure case has an invalid prediction identifier")
    closure = ClosureRequest.model_validate(value.get("submission"))
    raw_review = value.get("review")
    review = ClosureReviewRequest.model_validate(raw_review) if raw_review is not None else None
    return closure, prediction_id, review


def _metric_record_from_row(row: TelemetryRow) -> MetricRecord:
    record = MetricRecord.model_validate(row.payload)
    _require_row_metadata(
        "metric",
        {
            "id": row.id
            in {
                _record_id("metric", row.payload, run_id=row.run_id),
                _legacy_record_id("metric", row.payload),
            },
            "entity_id": row.entity_id == record.entity_id,
            "metric": row.metric == record.metric,
            "observed_at": _ensure_utc(row.observed_at) == record.observed_at,
        },
    )
    return record


def _app_probe_record_from_row(row: AppProbeRow) -> AppProbeRecord:
    record = AppProbeRecord.model_validate(row.payload)
    _require_row_metadata(
        "application probe",
        {
            "id": row.id
            in {
                _record_id("app_probe", row.payload, run_id=row.run_id),
                _legacy_record_id("app_probe", row.payload),
            },
            "entity_id": row.entity_id == record.entity_id,
            "observed_at": _ensure_utc(row.observed_at) == record.observed_at,
        },
    )
    return record


def _event_record_from_row(row: EventRow) -> EventRecord:
    record = EventRecord.model_validate(row.payload)
    _require_row_metadata(
        "event",
        {
            "id": row.id
            in {
                _record_id("event", row.payload, run_id=row.run_id),
                _legacy_record_id("event", row.payload),
            },
            "entity_id": row.entity_id == record.entity_id,
            "event_type": row.event_type == record.event_type,
            "observed_at": _ensure_utc(row.observed_at) == record.observed_at,
        },
    )
    return record


def _audit_event_from_row(row: AuditEventRow) -> AuditEvent:
    event = AuditEvent(
        occurred_at=_ensure_utc(row.occurred_at),
        actor_id=row.actor_id,
        event_type=row.event_type,
        detail=row.detail,
    )
    _require_row_metadata(
        "audit event",
        {
            "id": row.id
            in {
                _record_id("audit", _json_payload(event), context=row.incident_id),
                _legacy_record_id("audit", _json_payload(event)),
            },
        },
    )
    return event


def _require_row_metadata(record_type: str, checks: dict[str, bool]) -> None:
    mismatches = sorted(name for name, matches in checks.items() if not matches)
    if mismatches:
        raise ValueError(
            f"{record_type} metadata does not match its retained evidence: "
            + ", ".join(mismatches)
        )


def _closure_case_from_row(row: ClosureCaseRow) -> ClosureCase:
    case = ClosureCase.model_validate(row.payload)
    expected_status = case.review.decision if case.review is not None else "pending"
    checks = {
        "id": row.id == case.id,
        "incident_id": row.incident_id == case.incident_id,
        "prediction_id": row.prediction_id == case.prediction_id,
        "created_at": _ensure_utc(row.created_at) == case.created_at,
        "status": row.status == expected_status,
        "outcome": row.outcome == case.submission.outcome,
        "incident_type": row.incident_type == case.incident.prediction.likely_incident_type,
        "entity_id": row.entity_id == case.incident.prediction.entity_id,
        "network_profile_sha256": (
            row.network_profile_sha256 == case.incident.prediction.network_profile_sha256
        ),
    }
    _require_row_metadata("closure case normalized", checks)
    return case


def _action_operation_from_row(row: ActionOperationRow) -> ActionOperation:
    operation = ActionOperation.model_validate(row.payload)
    checks = {
        "id": row.id == operation.id,
        "incident_id": row.incident_id == operation.incident_id,
        "playbook_id": row.playbook_id == operation.playbook_id,
        "prediction_id": row.prediction_id == operation.prediction_id,
        "kind": row.kind == operation.kind,
        "status": row.status == operation.status,
        "created_at": _ensure_utc(row.created_at) == operation.created_at,
    }
    _require_row_metadata("action operation normalized", checks)
    return operation


def _incident_state_from_row(row: IncidentStateRow) -> StoredIncidentState:
    review_note, reviewed_prediction_id = _decode_review_state(row.review_note)
    closure, closure_prediction_id, closure_review = _decode_closure_state(row.closure_payload)
    case_id = (row.closure_payload or {}).get("case_id")
    _require_row_metadata(
        "incident state",
        {
            "review": (row.reviewed_by is None) == (review_note is None),
            "closure_case_id": case_id is None or isinstance(case_id, str),
            "independent_closure_review": (
                closure is None
                or closure_review is None
                or closure.actor_id != closure_review.actor_id
            ),
        },
    )
    return StoredIncidentState(
        reviewed_by=row.reviewed_by,
        review_note=review_note,
        reviewed_prediction_id=reviewed_prediction_id,
        closure=closure,
        closure_prediction_id=closure_prediction_id,
        closure_review=closure_review,
        closure_case_id=case_id if isinstance(case_id, str) else None,
    )


def _verify_incident_relationships(session: Session, *, batch_size: int) -> int:
    """Check workflow ownership and current-case references in one read transaction."""

    count = 0
    states = session.execute(
        select(IncidentStateRow, ActionOperationRow)
        .outerjoin(
            ActionOperationRow,
            IncidentStateRow.active_action_id == ActionOperationRow.id,
        )
        .order_by(IncidentStateRow.incident_id)
    ).yield_per(batch_size)
    for state_row, active_action_row in states:
        state = _incident_state_from_row(state_row)
        if state_row.active_action_id is not None:
            _require_row_metadata(
                "incident state relationship",
                {
                    "active_action_exists": active_action_row is not None,
                    "active_action_incident": (
                        active_action_row is not None
                        and active_action_row.incident_id == state_row.incident_id
                    ),
                    "active_action_status": (
                        active_action_row is not None
                        and active_action_row.status in {"pending", "unknown"}
                    ),
                },
            )
        if state.closure_case_id is not None:
            case_row = session.get(ClosureCaseRow, state.closure_case_id)
            case = _closure_case_from_row(case_row) if case_row is not None else None
            _require_row_metadata(
                "incident state relationship",
                {
                    "closure_case_exists": case is not None,
                    "closure_case_incident": (
                        case is not None and case.incident_id == state_row.incident_id
                    ),
                    "closure_case_prediction": (
                        case is not None and case.prediction_id == state.closure_prediction_id
                    ),
                    "closure_case_submission": (
                        case is not None and case.submission == state.closure
                    ),
                    "closure_case_review": (
                        case is not None and case.review == state.closure_review
                    ),
                },
            )
        count += 1

    unconfirmed_actions = session.execute(
        select(ActionOperationRow, IncidentStateRow)
        .outerjoin(
            IncidentStateRow,
            IncidentStateRow.incident_id == ActionOperationRow.incident_id,
        )
        .where(ActionOperationRow.status.in_(["pending", "unknown"]))
        .order_by(ActionOperationRow.id)
    ).yield_per(batch_size)
    for action_row, state_row in unconfirmed_actions:
        _require_row_metadata(
            "action operation relationship",
            {
                "incident_state_exists": state_row is not None,
                "incident_claim": (
                    state_row is not None and state_row.active_action_id == action_row.id
                ),
            },
        )
    return count


def _new_audit_event(actor_id: str, event_type: str, detail: str) -> AuditEvent:
    return AuditEvent(
        occurred_at=datetime.now(UTC),
        actor_id=actor_id,
        event_type=event_type,
        detail=detail,
    )


def _record_id(
    record_type: str,
    payload: dict[str, object],
    *,
    run_id: str | None = None,
    context: str | None = None,
) -> str:
    encoded = json.dumps(
        {
            "record_type": record_type,
            "payload": payload,
            "run_id": run_id,
            "context": context,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _legacy_record_id(record_type: str, payload: dict[str, object]) -> str:
    """Verify evidence written before run/context identity was introduced."""

    encoded = json.dumps(
        {"record_type": record_type, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
