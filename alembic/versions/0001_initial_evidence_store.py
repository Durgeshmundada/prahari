"""Create the initial PRAHARI evidence-store schema.

Revision ID: 0001_initial_evidence_store
Revises:
Create Date: 2026-08-20
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001_initial_evidence_store"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "telemetry_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=100), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_telemetry_records_entity_id", "telemetry_records", ["entity_id"])
    op.create_index("ix_telemetry_records_metric", "telemetry_records", ["metric"])
    op.create_index("ix_telemetry_records_observed_at", "telemetry_records", ["observed_at"])
    op.create_index("ix_telemetry_records_run_id", "telemetry_records", ["run_id"])

    op.create_table(
        "app_probe_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_app_probe_records_entity_id", "app_probe_records", ["entity_id"])
    op.create_index("ix_app_probe_records_observed_at", "app_probe_records", ["observed_at"])
    op.create_index("ix_app_probe_records_run_id", "app_probe_records", ["run_id"])

    op.create_table(
        "event_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=100), nullable=True),
        sa.Column("entity_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_event_records_entity_id", "event_records", ["entity_id"])
    op.create_index("ix_event_records_event_type", "event_records", ["event_type"])
    op.create_index("ix_event_records_observed_at", "event_records", ["observed_at"])
    op.create_index("ix_event_records_run_id", "event_records", ["run_id"])

    op.create_table(
        "incident_states",
        sa.Column("incident_id", sa.String(length=100), nullable=False),
        sa.Column("reviewed_by", sa.String(length=100), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("closure_payload", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("incident_id"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.String(length=100), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"])
    op.create_index("ix_audit_events_incident_id", "audit_events", ["incident_id"])
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("incident_states")
    op.drop_table("event_records")
    op.drop_table("app_probe_records")
    op.drop_table("telemetry_records")
