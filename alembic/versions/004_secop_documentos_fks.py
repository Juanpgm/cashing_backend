"""feat: add FK links from secop_documentos to secop_contratos and secop_procesos

Revision ID: 004_secop_documentos_fks
Revises: 003_documento_fuente_contrato_id
Create Date: 2026-03-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_secop_documentos_fks"
down_revision = "003_documento_fuente_contrato_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "secop_documentos",
        sa.Column("secop_contrato_id", sa.Uuid(), sa.ForeignKey("secop_contratos.id"), nullable=True),
    )
    op.add_column(
        "secop_documentos",
        sa.Column("secop_proceso_id", sa.Uuid(), sa.ForeignKey("secop_procesos.id"), nullable=True),
    )
    op.create_index("ix_secop_docs_contrato_fk", "secop_documentos", ["secop_contrato_id"])
    op.create_index("ix_secop_docs_proceso_fk", "secop_documentos", ["secop_proceso_id"])


def downgrade() -> None:
    op.drop_index("ix_secop_docs_proceso_fk", table_name="secop_documentos")
    op.drop_index("ix_secop_docs_contrato_fk", table_name="secop_documentos")
    op.drop_column("secop_documentos", "secop_proceso_id")
    op.drop_column("secop_documentos", "secop_contrato_id")
