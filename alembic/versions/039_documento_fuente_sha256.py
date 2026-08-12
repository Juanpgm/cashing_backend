"""feat(documentos): add sha256 column for upload_document content dedup

Adds a nullable `sha256` hex-digest column to `documentos_fuente` so
`document_service.upload_document` can detect a re-upload of the same
content under a DIFFERENT filename BEFORE writing a new row or storage
object (mirrors migration 038's `evidencias.sha256`). No backfill —
pre-existing rows stay NULL and simply don't participate in hash dedup.

Revision ID: 039_documento_fuente_sha256
Revises: 038_evidencia_sha256
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "039_documento_fuente_sha256"
down_revision = "038_evidencia_sha256"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documentos_fuente", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.create_index("ix_documentos_fuente_sha256", "documentos_fuente", ["sha256"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_documentos_fuente_sha256", table_name="documentos_fuente")
    op.drop_column("documentos_fuente", "sha256")
