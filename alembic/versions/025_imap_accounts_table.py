"""feat(imap): generic IMAP mailbox credential store

Creates `imap_accounts` (host/port/username/password_encrypted/use_ssl,
`UniqueConstraint(usuario_id, email)`) for mailboxes not hosted on Google or
Microsoft 365 (custom-domain/on-prem IMAP servers). Kept separate from
`integraciones` — that table's columns are OAuth-shaped (access/refresh
token, scopes, expiry) and its CHECK constraint only allows
provider IN ('google', 'microsoft'); IMAP has none of those concepts.

Revision ID: 025_imap_accounts_table
Revises: 024_integraciones_table
Create Date: 2026-07-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025_imap_accounts_table"
down_revision = "024_integraciones_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "imap_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("usuario_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False, server_default="993"),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("use_ssl", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usuario_id", "email", name="uq_imap_accounts_usuario_email"),
    )
    op.create_index("ix_imap_accounts_usuario_id", "imap_accounts", ["usuario_id"])


def downgrade() -> None:
    op.drop_index("ix_imap_accounts_usuario_id", table_name="imap_accounts")
    op.drop_table("imap_accounts")
