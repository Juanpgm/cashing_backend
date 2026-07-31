"""feat(evidencias-ai-classification): add evidencia_obligacion link table

Adds `evidencia_obligacion` (evidence-obligation-links, slice #2): a NEW
many-to-many link table between `evidencias` and `obligaciones`, carrying
per-link `confianza` (alta/media/baja), an optional numeric `score`, free-text
`reasoning`, `status` (proposed/confirmed/rejected/no_aplica) and `source`
(ai/user). `obligacion_id` is nullable to represent the "no obligación
applies" state as a row scoped to the evidencia with no specific obligación.

New-table-only migration: does NOT alter any column on `evidencias` or
`obligaciones` — safe under local SQLite `create_all` (the model is additive,
so a fresh `local_dev.db` picks it up automatically on next boot) and mirrored
here for Postgres.

Revision ID: 030_evidencia_obligacion_link
Revises: 029_contexto_usuario_evidencia_texto
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "030_evidencia_obligacion_link"
down_revision = "029_contexto_usuario_evidencia_texto"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evidencia_obligacion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("evidencia_id", sa.Uuid(), nullable=False),
        sa.Column("obligacion_id", sa.Uuid(), nullable=True),
        sa.Column("confianza", sa.String(length=10), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="proposed"),
        sa.Column("source", sa.String(length=10), nullable=False, server_default="ai"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["evidencia_id"], ["evidencias.id"], name="fk_evidencia_obligacion_evidencia_id"),
        sa.ForeignKeyConstraint(["obligacion_id"], ["obligaciones.id"], name="fk_evidencia_obligacion_obligacion_id"),
        sa.UniqueConstraint("evidencia_id", "obligacion_id", name="uq_evidencia_obligacion"),
    )
    op.create_index("ix_evidencia_obligacion_evidencia_id", "evidencia_obligacion", ["evidencia_id"])
    op.create_index("ix_evidencia_obligacion_obligacion_id", "evidencia_obligacion", ["obligacion_id"])


def downgrade() -> None:
    op.drop_index("ix_evidencia_obligacion_obligacion_id", table_name="evidencia_obligacion")
    op.drop_index("ix_evidencia_obligacion_evidencia_id", table_name="evidencia_obligacion")
    op.drop_table("evidencia_obligacion")
