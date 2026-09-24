"""Indexed normalized closure-case search metadata."""

import json

import sqlalchemy as sa

from alembic import op

revision = "0005_case_search"
down_revision = "0004_action_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {
        "outcome": sa.String(30),
        "incident_type": sa.String(100),
        "entity_id": sa.String(200),
        "network_profile_sha256": sa.String(64),
    }
    for name, column_type in columns.items():
        op.add_column("closure_cases", sa.Column(name, column_type, nullable=True))

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id, payload FROM closure_cases")).mappings()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        prediction = payload["incident"]["prediction"]
        connection.execute(
            sa.text(
                "UPDATE closure_cases SET outcome=:outcome, incident_type=:incident_type, "
                "entity_id=:entity_id, network_profile_sha256=:profile WHERE id=:id"
            ),
            {
                "id": row["id"],
                "outcome": payload["submission"]["outcome"],
                "incident_type": prediction["likely_incident_type"],
                "entity_id": prediction["entity_id"],
                "profile": prediction.get("network_profile_sha256"),
            },
        )

    with op.batch_alter_table("closure_cases") as batch:
        batch.alter_column("outcome", existing_type=sa.String(30), nullable=False)
        batch.alter_column("incident_type", existing_type=sa.String(100), nullable=False)
        batch.alter_column("entity_id", existing_type=sa.String(200), nullable=False)

    for name in columns:
        op.create_index(f"ix_closure_cases_{name}", "closure_cases", [name])


def downgrade() -> None:
    for name in ("network_profile_sha256", "entity_id", "incident_type", "outcome"):
        op.drop_index(f"ix_closure_cases_{name}", table_name="closure_cases")
        op.drop_column("closure_cases", name)
