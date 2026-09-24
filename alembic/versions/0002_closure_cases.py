"""Retain closure evidence and independent review decisions.

Revision ID: 0002_closure_cases
Revises: 0001_initial_evidence_store
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_closure_cases"
down_revision = "0001_initial_evidence_store"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "closure_cases",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("incident_id", sa.String(100), nullable=False),
        sa.Column("prediction_id", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    for column in ("incident_id", "created_at", "status"):
        op.create_index(f"ix_closure_cases_{column}", "closure_cases", [column])


def downgrade() -> None:
    op.drop_table("closure_cases")
