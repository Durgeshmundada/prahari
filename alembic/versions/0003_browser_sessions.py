"""Revocable browser sessions and bounded login rate windows."""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_browser_sessions"
down_revision = "0002_closure_cases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(100), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("credential_fingerprint", sa.String(64), nullable=False),
        sa.Column("csrf_token", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_browser_sessions_username", "browser_sessions", ["username"])
    op.create_index("ix_browser_sessions_expires_at", "browser_sessions", ["expires_at"])
    op.create_table(
        "login_windows",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("login_windows")
    op.drop_table("browser_sessions")
