"""Durable action outcomes and incident dispatch ownership."""

import sqlalchemy as sa

from alembic import op

revision = "0004_action_operations"
down_revision = "0003_browser_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    legacy_intents = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM audit_events "
                "WHERE event_type IN ('action_apply_requested', 'rollback_requested')"
            )
        )
        .scalar_one()
    )
    if legacy_intents:
        # Old intent/result events had no operation correlation ID. A terminal
        # event cannot prove which command it acknowledges. Never infer safety
        # by silently importing them as completed or dropping their uncertainty.
        raise RuntimeError(
            "legacy device intents require an approved migration and independent "
            "route reconciliation procedure before 0004_action_operations; preserve audit evidence"
        )
    op.add_column("incident_states", sa.Column("active_action_id", sa.String(100), nullable=True))
    op.create_table(
        "action_operations",
        sa.Column("id", sa.String(100), primary_key=True),
        sa.Column("incident_id", sa.String(100), nullable=False),
        sa.Column("playbook_id", sa.String(100), nullable=False),
        sa.Column("prediction_id", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "incident_id", "playbook_id", "prediction_id", "kind", name="uq_action_prediction_kind"
        ),
    )
    for field in ("incident_id", "status", "created_at"):
        op.create_index(f"ix_action_operations_{field}", "action_operations", [field])


def downgrade() -> None:
    op.drop_table("action_operations")
    op.drop_column("incident_states", "active_action_id")
