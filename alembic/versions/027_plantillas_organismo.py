"""feat(plantilla-organismo): add per-organism template structure table

Adds `plantillas_organismo` (billing-resilience-templates, slice #5): persists
the STRUCTURE (columns/sections/anexo refs) extracted from an ingested
institutional informe template, keyed to `(usuario_id, entidad_normalizada,
tipo_documento)`. `estructura_json` is Postgres JSONB (the model uses plain
JSON for aiosqlite test-DB compatibility — same pattern as
`agent_checkpoints.state_json`, migration 012).

No new Postgres enum type is created by this migration — `tipo_documento` and
`formato` are plain `VARCHAR` (design D5's literal field shape), avoiding the
enum-label `values_callable` gotcha class entirely for this table.

Revision ID: 027_plantillas_organismo
Revises: 026_adiciones_contrato
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "027_plantillas_organismo"
down_revision = "026_adiciones_contrato"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plantillas_organismo",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("usuario_id", sa.Uuid(), nullable=False),
        sa.Column("entidad", sa.String(length=255), nullable=False),
        sa.Column("entidad_normalizada", sa.String(length=255), nullable=False),
        sa.Column("tipo_documento", sa.String(length=50), nullable=False),
        sa.Column("formato", sa.String(length=20), nullable=False),
        sa.Column("estructura_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fuente_documento_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], name="fk_plantillas_organismo_usuario_id"),
        sa.ForeignKeyConstraint(
            ["fuente_documento_id"],
            ["documentos_fuente.id"],
            name="fk_plantillas_organismo_fuente_documento_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("usuario_id", "entidad_normalizada", "tipo_documento", name="uq_plantilla_organismo_key"),
    )
    op.create_index("ix_plantillas_organismo_usuario_id", "plantillas_organismo", ["usuario_id"])
    op.create_index("ix_plantillas_organismo_entidad_normalizada", "plantillas_organismo", ["entidad_normalizada"])


def downgrade() -> None:
    op.drop_index("ix_plantillas_organismo_entidad_normalizada", table_name="plantillas_organismo")
    op.drop_index("ix_plantillas_organismo_usuario_id", table_name="plantillas_organismo")
    op.drop_table("plantillas_organismo")
