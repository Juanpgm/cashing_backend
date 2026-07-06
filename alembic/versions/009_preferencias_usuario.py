"""feat: create preferencias_usuario table (Phase 7)

Revision ID: 009_preferencias_usuario
Revises: 008_pgvector_embeddings
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "009_preferencias_usuario"
down_revision = "008_pgvector_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "preferencias_usuario",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("usuario_id", sa.Uuid(), nullable=False),
        sa.Column(
            "clave",
            sa.String(length=100),
            nullable=False,
            comment="Preference key, e.g. 'idioma', 'modo_agente_default'",
        ),
        sa.Column(
            "valor",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="JSON value — string, number, bool, list, or dict",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["usuario_id"],
            ["usuarios.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("usuario_id", "clave", name="uq_preferencias_usuario_clave"),
    )
    op.create_index(
        "ix_preferencias_usuario_usuario_id",
        "preferencias_usuario",
        ["usuario_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_preferencias_usuario_usuario_id", table_name="preferencias_usuario")
    op.drop_table("preferencias_usuario")
