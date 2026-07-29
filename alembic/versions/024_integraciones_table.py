"""feat(integraciones): generalized multi-provider credential store

Creates the `integraciones` table (provider discriminator google|microsoft,
Fernet-encrypted tokens, `UniqueConstraint(usuario_id, provider, email)`) and
backfills every existing `google_tokens` row into it with `provider='google'`,
preserving the encrypted token material/expiry so already-connected Google
users need no re-consent (integraciones spec: "Existing connected Google user
keeps working after migration").

`google_tokens` has no `email` column, so every backfilled row gets `email=""`
— consistent with design.md D5's uniqueness refinement (unknown-email
connections collapse to one row per (usuario_id, provider)).

`google_tokens` is kept intact and read-only — NOT dropped here (design D5:
dropping is a separate follow-up migration gated on verified prod cutover).

Revision ID: 024_integraciones_table
Revises: 023_documento_requisito_vinculos
Create Date: 2026-07-28
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "024_integraciones_table"
down_revision = "023_documento_requisito_vinculos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integraciones",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("usuario_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=False),
        sa.Column("scopes", sa.String(length=1000), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usuario_id", "provider", "email", name="uq_integraciones_usuario_provider_email"),
        sa.CheckConstraint("provider IN ('google', 'microsoft')", name="ck_integraciones_provider"),
    )
    op.create_index("ix_integraciones_usuario_provider", "integraciones", ["usuario_id", "provider"])

    # ── Backfill: copy every existing google_tokens row as provider='google' ──
    bind = op.get_bind()
    google_tokens = sa.table(
        "google_tokens",
        sa.column("usuario_id", sa.Uuid()),
        sa.column("access_token_encrypted", sa.Text()),
        sa.column("refresh_token_encrypted", sa.Text()),
        sa.column("scopes", sa.String()),
        sa.column("expires_at", sa.DateTime(timezone=True)),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    integraciones = sa.table(
        "integraciones",
        sa.column("id", sa.Uuid()),
        sa.column("usuario_id", sa.Uuid()),
        sa.column("provider", sa.String()),
        sa.column("access_token_encrypted", sa.Text()),
        sa.column("refresh_token_encrypted", sa.Text()),
        sa.column("scopes", sa.String()),
        sa.column("expires_at", sa.DateTime(timezone=True)),
        sa.column("email", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    if bind.dialect.name == "postgresql":
        # Single set-based INSERT ... SELECT (design.md's original intent) — zero
        # per-row round trips and minimal transaction/lock duration on real Postgres.
        # gen_random_uuid() is a Postgres 13+ built-in (no pgcrypto extension needed).
        bind.execute(
            integraciones.insert().from_select(
                [
                    "id",
                    "usuario_id",
                    "provider",
                    "access_token_encrypted",
                    "refresh_token_encrypted",
                    "scopes",
                    "expires_at",
                    "email",
                    "created_at",
                    "updated_at",
                ],
                sa.select(
                    sa.literal_column("gen_random_uuid()"),
                    google_tokens.c.usuario_id,
                    sa.literal("google"),
                    google_tokens.c.access_token_encrypted,
                    google_tokens.c.refresh_token_encrypted,
                    google_tokens.c.scopes,
                    google_tokens.c.expires_at,
                    sa.literal(""),
                    google_tokens.c.created_at,
                    google_tokens.c.updated_at,
                ),
            )
        )
    else:
        # ponytail: gen_random_uuid() has no SQLite equivalent, and this branch only
        # exists so alembic/versions/024's upgrade() is exercisable against a throwaway
        # aiosqlite DB in tests (see tests/test_migration_024_backfill.py). Still a
        # single bulk INSERT (one execute() call, not N) — round-trip cost is the same
        # concern the Postgres branch above addresses. Upgrade path: none needed, real
        # deployments always run on Postgres (bind.dialect.name == "postgresql" above).
        rows = bind.execute(
            sa.select(
                google_tokens.c.usuario_id,
                google_tokens.c.access_token_encrypted,
                google_tokens.c.refresh_token_encrypted,
                google_tokens.c.scopes,
                google_tokens.c.expires_at,
                google_tokens.c.created_at,
                google_tokens.c.updated_at,
            )
        ).fetchall()
        if rows:
            bind.execute(
                integraciones.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "usuario_id": row.usuario_id,
                        "provider": "google",
                        "access_token_encrypted": row.access_token_encrypted,
                        "refresh_token_encrypted": row.refresh_token_encrypted,
                        "scopes": row.scopes,
                        "expires_at": row.expires_at,
                        "email": "",
                        "created_at": row.created_at,
                        "updated_at": row.updated_at,
                    }
                    for row in rows
                ],
            )


def downgrade() -> None:
    """Drop `integraciones` — DESTRUCTIVE, NOT lossless in general.

    Safe (no data loss) ONLY if this runs BEFORE any post-cutover write, i.e.
    before Slice A1's code cutover (`integration_service`/adapters reading and
    writing `integraciones`) goes live. Once live:
    - Any Google row created/refreshed after cutover has no equivalent write-back
      to `google_tokens` (the cutover is one-way), so it is silently destroyed.
    - Any Microsoft row has NO fallback table at all — `google_tokens` only ever
      held Google credentials — so it is unconditionally lost.
    Do not run this downgrade on an environment that has taken traffic since the
    corresponding upgrade without first confirming no such rows exist.
    """
    op.drop_index("ix_integraciones_usuario_provider", table_name="integraciones")
    op.drop_table("integraciones")
