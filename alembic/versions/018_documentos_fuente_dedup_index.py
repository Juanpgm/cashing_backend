"""feat(documentos): add composite deduplication index to documentos_fuente

Revision ID: 018_documentos_fuente_dedup_index
Revises: 017_categoria_documento
Create Date: 2026-06-24
"""

from __future__ import annotations

from alembic import op

revision = "018_documentos_fuente_dedup_index"
down_revision = "017_categoria_documento"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_documentos_fuente_dedup",
        "documentos_fuente",
        ["usuario_id", "nombre", "tipo", "contrato_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_documentos_fuente_dedup", table_name="documentos_fuente")
