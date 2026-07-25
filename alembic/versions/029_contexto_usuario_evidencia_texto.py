"""feat(justificaciones): user monthly context + evidence extracted text + google email

Three additive nullable columns (justification-generation improvements):

- `cuentas_cobro.contexto_usuario` (TEXT) — free-text user summary of the month,
  optional grounding for every LLM generation path.
- `evidencias.texto_extraido` (TEXT) — extracted text content of uploaded evidence
  files, populated at upload time so generation can ground on file content.
- `google_tokens.email` (VARCHAR 320) — connected Google account email, captured
  at OAuth connect time for /integraciones/google/status display.

All nullable, non-breaking: legacy rows load with NULL.

Revision ID: 029_contexto_usuario_evidencia_texto
Revises: 028_cuenta_cobro_fecha_transaccion
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "029_contexto_usuario_evidencia_texto"
down_revision = "028_cuenta_cobro_fecha_transaccion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cuentas_cobro", sa.Column("contexto_usuario", sa.Text(), nullable=True))
    op.add_column("evidencias", sa.Column("texto_extraido", sa.Text(), nullable=True))
    op.add_column("google_tokens", sa.Column("email", sa.String(length=320), nullable=True))


def downgrade() -> None:
    op.drop_column("google_tokens", "email")
    op.drop_column("evidencias", "texto_extraido")
    op.drop_column("cuentas_cobro", "contexto_usuario")
