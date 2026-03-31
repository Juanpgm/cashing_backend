"""fix: add failed_login_attempts column to usuarios table

Revision ID: 005_usuarios_failed_login_attempts
Revises: 004_secop_documentos_fks
Create Date: 2026-03-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "005_usuarios_failed_login_attempts"
down_revision = "004_secop_documentos_fks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usuarios",
        sa.Column(
            "failed_login_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("usuarios", "failed_login_attempts")
