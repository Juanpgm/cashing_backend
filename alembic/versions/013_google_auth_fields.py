"""feat(auth): Google OAuth fields on usuarios — google_id, photo_url, provider, nullable password_hash

Revision ID: 013_google_auth_fields
Revises: 012_agent_checkpoints
Create Date: 2026-06-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "013_google_auth_fields"
down_revision: str = "012_agent_checkpoints"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add Google OAuth columns
    op.add_column("usuarios", sa.Column("google_id", sa.String(128), nullable=True))
    op.add_column("usuarios", sa.Column("photo_url", sa.String(500), nullable=True))
    op.add_column(
        "usuarios",
        sa.Column(
            "provider",
            sa.String(20),
            nullable=False,
            server_default="email",
        ),
    )

    # Unique index on google_id (partial — only for non-NULL rows)
    op.create_index(
        "ix_usuarios_google_id",
        "usuarios",
        ["google_id"],
        unique=True,
        postgresql_where=sa.text("google_id IS NOT NULL"),
    )

    # Make password_hash nullable (OAuth users have no password)
    op.alter_column("usuarios", "password_hash", nullable=True)


def downgrade() -> None:
    # Restore password_hash as NOT NULL (will fail if any row has NULL — must clean up first)
    op.alter_column("usuarios", "password_hash", nullable=False)
    op.drop_index("ix_usuarios_google_id", table_name="usuarios")
    op.drop_column("usuarios", "provider")
    op.drop_column("usuarios", "photo_url")
    op.drop_column("usuarios", "google_id")
