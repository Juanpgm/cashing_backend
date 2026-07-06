"""feat: create google_tokens table for OAuth token storage

Revision ID: 006_google_tokens_table
Revises: 005_usuarios_failed_login_attempts
Create Date: 2026-04-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "006_google_tokens_table"
down_revision = "005_usuarios_failed_login_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "google_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("usuario_id", sa.Uuid(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("scopes", sa.String(length=500), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usuario_id"),
    )
    op.create_index("ix_google_tokens_usuario_id", "google_tokens", ["usuario_id"])


def downgrade() -> None:
    op.drop_index("ix_google_tokens_usuario_id", table_name="google_tokens")
    op.drop_table("google_tokens")
