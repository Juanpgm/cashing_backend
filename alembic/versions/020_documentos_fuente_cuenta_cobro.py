"""feat(documentos): scope documentos_fuente to a cuenta de cobro (strict per-cuenta isolation)

Adds `cuenta_cobro_id` so uploaded/generated documents belong to exactly one cuenta and
never leak into another cuenta of the same contract. Backfills existing rows from the
checklist link and widens the dedup index so the same file can exist per cuenta.

Revision ID: 020_documentos_fuente_cuenta_cobro
Revises: 019_requisitos_cuenta
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "020_documentos_fuente_cuenta_cobro"
down_revision = "019_requisitos_cuenta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("documentos_fuente", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cuenta_cobro_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_docfuente_cuenta_cobro",
            "cuentas_cobro",
            ["cuenta_cobro_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_index("ix_documentos_fuente_cuenta_cobro_id", ["cuenta_cobro_id"])

    # Backfill: scope each already-linked document to the cuenta whose checklist links it.
    op.execute(
        """
        UPDATE documentos_fuente
        SET cuenta_cobro_id = (
            SELECT dcc.cuenta_cobro_id
            FROM documentos_cuenta_cobro dcc
            WHERE dcc.documento_fuente_id = documentos_fuente.id
            LIMIT 1
        )
        WHERE cuenta_cobro_id IS NULL
        """
    )

    # Widen the dedup index so the same filename can exist independently per cuenta.
    op.drop_index("ix_documentos_fuente_dedup", table_name="documentos_fuente")
    op.create_index(
        "ix_documentos_fuente_dedup",
        "documentos_fuente",
        ["usuario_id", "nombre", "tipo", "contrato_id", "cuenta_cobro_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_documentos_fuente_dedup", table_name="documentos_fuente")
    op.create_index(
        "ix_documentos_fuente_dedup",
        "documentos_fuente",
        ["usuario_id", "nombre", "tipo", "contrato_id"],
    )
    with op.batch_alter_table("documentos_fuente", schema=None) as batch_op:
        batch_op.drop_index("ix_documentos_fuente_cuenta_cobro_id")
        batch_op.drop_constraint("fk_docfuente_cuenta_cobro", type_="foreignkey")
        batch_op.drop_column("cuenta_cobro_id")
