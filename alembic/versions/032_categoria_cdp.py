"""feat(checklist): add 'cdp' value to categoria_documento enum

CDP becomes a first-class CategoriaDocumento (clasificacion-documentos-secop,
design D1). Isolated in its OWN migration: Postgres requires that a value added
with ALTER TYPE ... ADD VALUE is not used in the same transaction, so no other
DDL/DML shares this revision.

SQLite (local dev / tests) stores this enum as VARCHAR without a CHECK
constraint (SQLAlchemy 2.x default create_constraint=False), so the dialect
guard makes this a no-op there.

Revision ID: 032_categoria_cdp
Revises: 031_clasificacion_job
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op

revision = "032_categoria_cdp"
down_revision = "031_clasificacion_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE categoria_documento ADD VALUE IF NOT EXISTS 'cdp'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type; the added value is
    # inert if unused. Intentional no-op.
    pass
