"""feat(checklist): content-sniff memoization columns + candidato score provenance

clasificacion-documentos-secop, design D2. Three nullable, additive columns:

- secop_documentos.texto_extraido (Text): extracted text (truncated ~20k chars)
  so a SECOP file is downloaded at most once ever; re-scans re-score for free.
- secop_documentos.texto_estado (String): 'ok' | 'sin_texto' | 'error'.
- documento_checklist_candidatos.score_origen (String): 'nombre' | 'contenido'
  — which signal produced the candidate's final score.

Rollback = revert code; columns are inert.

Revision ID: 033_sniff_contenido
Revises: 032_categoria_cdp
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "033_sniff_contenido"
down_revision = "032_categoria_cdp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("secop_documentos", sa.Column("texto_extraido", sa.Text(), nullable=True))
    op.add_column("secop_documentos", sa.Column("texto_estado", sa.String(length=10), nullable=True))
    op.add_column("documento_checklist_candidatos", sa.Column("score_origen", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("documento_checklist_candidatos", "score_origen")
    op.drop_column("secop_documentos", "texto_estado")
    op.drop_column("secop_documentos", "texto_extraido")
