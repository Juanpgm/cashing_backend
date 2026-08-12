"""feat(checklist): add 'cdp' value to tipo_documento_fuente enum

Bug fix: `checklist_service._CATALOGO_SEED` has always seeded the CDP requisito
with `tipo_documento_fuente: "cdp"`, but the `TipoDocumentoFuente` enum never
had a `cdp` member — uploading a CDP document via `tipo=cdp` was impossible.
Isolated in its OWN migration, same as 032_categoria_cdp: Postgres requires
that a value added with ALTER TYPE ... ADD VALUE is not used in the same
transaction, so no other DDL/DML shares this revision.

SQLite (local dev / tests) stores this enum as VARCHAR without a CHECK
constraint (SQLAlchemy 2.x default create_constraint=False), so the dialect
guard makes this a no-op there.

Revision ID: 037_tipo_documento_fuente_cdp
Revises: 036_actividad_justificacion_origen
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "037_tipo_documento_fuente_cdp"
down_revision = "036_actividad_justificacion_origen"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE tipo_documento_fuente ADD VALUE IF NOT EXISTS 'cdp'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type; the added value is
    # inert if unused. Intentional no-op.
    pass
