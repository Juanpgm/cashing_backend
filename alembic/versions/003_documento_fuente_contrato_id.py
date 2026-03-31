"""feat: add contrato_id to documentos_fuente

Revision ID: 003_documento_fuente_contrato_id
Revises: 002_contrato_documento_proveedor
Create Date: 2026-03-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "003_documento_fuente_contrato_id"
down_revision = "002_contrato_documento_proveedor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documentos_fuente",
        sa.Column("contrato_id", sa.Uuid(), sa.ForeignKey("contratos.id"), nullable=True),
    )
    op.create_index("ix_documentos_fuente_contrato_id", "documentos_fuente", ["contrato_id"])


def downgrade() -> None:
    op.drop_index("ix_documentos_fuente_contrato_id", table_name="documentos_fuente")
    op.drop_column("documentos_fuente", "contrato_id")
