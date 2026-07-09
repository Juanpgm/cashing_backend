"""feat(checklist): 1:N document links per requisito

Adds ``documento_requisito_vinculos`` so a single checklist requisito
(``DocumentoCuentaCobro``) can hold MULTIPLE linked documents instead of at most
one via the singular ``documento_fuente_id`` / ``secop_documento_id`` FKs. Those
singular FKs are kept as "the primary link" for backward compatibility; every
link (primary and secondary) is now also represented as a row in this table.

Data migration: seeds one vinculo row for every existing DocumentoCuentaCobro
that already has a primary link, preserving current links under the new model.

Revision ID: 023_documento_requisito_vinculos
Revises: 022_evidencia_link_fields
Create Date: 2026-07-08
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "023_documento_requisito_vinculos"
down_revision = "022_evidencia_link_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documento_requisito_vinculos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("documento_cuenta_cobro_id", sa.Uuid(), nullable=False),
        sa.Column("documento_fuente_id", sa.Uuid(), nullable=True),
        sa.Column("secop_documento_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["documento_cuenta_cobro_id"], ["documentos_cuenta_cobro.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["documento_fuente_id"], ["documentos_fuente.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["secop_documento_id"], ["secop_documentos.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("documento_cuenta_cobro_id", "documento_fuente_id", name="uq_docreqvinc_docccobro_fuente"),
        sa.UniqueConstraint("documento_cuenta_cobro_id", "secop_documento_id", name="uq_docreqvinc_docccobro_secop"),
        sa.CheckConstraint(
            "(documento_fuente_id IS NULL) <> (secop_documento_id IS NULL)",
            name="ck_docreqvinc_una_fuente",
        ),
    )
    op.create_index(
        "ix_documento_requisito_vinculos_documento_cuenta_cobro_id",
        "documento_requisito_vinculos",
        ["documento_cuenta_cobro_id"],
    )

    # ── Backfill: seed a vinculo row for every existing primary link ───────────
    bind = op.get_bind()
    docccobro = sa.table(
        "documentos_cuenta_cobro",
        sa.column("id", sa.Uuid()),
        sa.column("documento_fuente_id", sa.Uuid()),
        sa.column("secop_documento_id", sa.Uuid()),
    )
    vinculos = sa.table(
        "documento_requisito_vinculos",
        sa.column("id", sa.Uuid()),
        sa.column("documento_cuenta_cobro_id", sa.Uuid()),
        sa.column("documento_fuente_id", sa.Uuid()),
        sa.column("secop_documento_id", sa.Uuid()),
    )
    existentes = bind.execute(
        sa.select(
            docccobro.c.id,
            docccobro.c.documento_fuente_id,
            docccobro.c.secop_documento_id,
        ).where(
            sa.or_(
                docccobro.c.documento_fuente_id.isnot(None),
                docccobro.c.secop_documento_id.isnot(None),
            )
        )
    ).fetchall()
    for row in existentes:
        bind.execute(
            vinculos.insert().values(
                id=uuid.uuid4(),
                documento_cuenta_cobro_id=row.id,
                documento_fuente_id=row.documento_fuente_id,
                secop_documento_id=row.secop_documento_id,
            )
        )


def downgrade() -> None:
    op.drop_index(
        "ix_documento_requisito_vinculos_documento_cuenta_cobro_id",
        table_name="documento_requisito_vinculos",
    )
    op.drop_table("documento_requisito_vinculos")
