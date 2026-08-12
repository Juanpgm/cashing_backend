"""feat(evidencia): add sha256 column for upload dedup

Adds a nullable `sha256` hex-digest column to `evidencias` so upload sites
(`subir_evidencia`, `subir_evidencias`, `subir_evidencias_cuenta`) can detect
a re-upload of the same file (by content, not filename) BEFORE writing a new
row or storage object. No backfill — pre-existing rows stay NULL and simply
don't participate in dedup.

Revision ID: 038_evidencia_sha256
Revises: 037_tipo_documento_fuente_cdp
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "038_evidencia_sha256"
down_revision = "037_tipo_documento_fuente_cdp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("evidencias", sa.Column("sha256", sa.String(length=64), nullable=True))
    op.create_index("ix_evidencias_sha256", "evidencias", ["sha256"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_evidencias_sha256", table_name="evidencias")
    op.drop_column("evidencias", "sha256")
