"""feat: waitlist invite-code gate (invite_codes)

Adds the ``invite_codes`` table backing the optional waitlist gate. When
``settings.WAITLIST_ENABLED`` is True, account creation (email registration and
first-time Google sign-in) requires a valid, active, non-exhausted code; each
successful signup consumes one use.

Revision ID: 021_invite_codes
Revises: 020_documentos_fuente_cuenta_cobro
Create Date: 2026-07-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "021_invite_codes"
down_revision = "020_documentos_fuente_cuenta_cobro"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(length=64), nullable=False),
        sa.Column("max_usos", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("usos_actuales", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("nota", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("codigo", name="uq_invite_codes_codigo"),
    )
    op.create_index("ix_invite_codes_codigo", "invite_codes", ["codigo"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_invite_codes_codigo", table_name="invite_codes")
    op.drop_table("invite_codes")
