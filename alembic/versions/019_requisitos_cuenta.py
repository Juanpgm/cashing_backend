"""feat: per-cuenta custom/inferred requirements (requisitos_cuenta)

Adds a per-cuenta definition table for custom requirements inferred from the
contracting entity's document (or pasted text), lets the checklist state table
reference either a standard catalog code OR a custom requisito, and records the
checklist build mode on the cuenta (NULL until the post-creation gate is resolved).

Revision ID: 019_requisitos_cuenta
Revises: 018_documentos_fuente_dedup_index
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "019_requisitos_cuenta"
down_revision = "018_documentos_fuente_dedup_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── requisitos_cuenta (per-cuenta custom requirement definitions) ────────
    op.create_table(
        "requisitos_cuenta",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cuenta_cobro_id", sa.Uuid(), nullable=False),
        sa.Column("codigo", sa.String(length=50), nullable=False),
        sa.Column("etiqueta", sa.String(length=200), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("obligatorio", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("solo_primera_cuenta", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("tipo_documento_fuente", sa.String(length=50), nullable=True),
        sa.Column("keywords_deteccion", sa.JSON(), nullable=False),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("mapea_a_estandar", sa.String(length=50), nullable=True),
        sa.Column("origen", sa.String(length=20), nullable=False, server_default="inferido"),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["cuenta_cobro_id"], ["cuentas_cobro.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cuenta_cobro_id", "codigo", name="uq_reqcuenta_cuenta_codigo"),
    )
    op.create_index("ix_requisitos_cuenta_cuenta_cobro_id", "requisitos_cuenta", ["cuenta_cobro_id"])

    # ── documentos_cuenta_cobro: allow custom rows ───────────────────────────
    op.alter_column(
        "documentos_cuenta_cobro",
        "requisito_codigo",
        existing_type=sa.String(length=50),
        nullable=True,
    )
    op.add_column(
        "documentos_cuenta_cobro",
        sa.Column("requisito_cuenta_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_documentos_cuenta_cobro_requisito_cuenta_id",
        "documentos_cuenta_cobro",
        ["requisito_cuenta_id"],
    )
    op.create_foreign_key(
        "fk_docccobro_requisito_cuenta",
        "documentos_cuenta_cobro",
        "requisitos_cuenta",
        ["requisito_cuenta_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_docccobro_cuenta_reqcuenta",
        "documentos_cuenta_cobro",
        ["cuenta_cobro_id", "requisito_cuenta_id"],
    )
    op.create_check_constraint(
        "ck_docccobro_una_definicion",
        "documentos_cuenta_cobro",
        "(requisito_codigo IS NULL) <> (requisito_cuenta_id IS NULL)",
    )

    # ── cuentas_cobro.requisitos_modo ────────────────────────────────────────
    op.add_column(
        "cuentas_cobro",
        sa.Column("requisitos_modo", sa.String(length=20), nullable=True),
    )
    # Existing cuentas already have a materialised standard checklist, so they
    # must not fall into the gate: mark them as 'estandar'.
    op.execute("UPDATE cuentas_cobro SET requisitos_modo = 'estandar'")


def downgrade() -> None:
    op.drop_column("cuentas_cobro", "requisitos_modo")

    op.drop_constraint("ck_docccobro_una_definicion", "documentos_cuenta_cobro", type_="check")
    op.drop_constraint("uq_docccobro_cuenta_reqcuenta", "documentos_cuenta_cobro", type_="unique")
    op.drop_constraint("fk_docccobro_requisito_cuenta", "documentos_cuenta_cobro", type_="foreignkey")
    op.drop_index(
        "ix_documentos_cuenta_cobro_requisito_cuenta_id",
        table_name="documentos_cuenta_cobro",
    )
    op.drop_column("documentos_cuenta_cobro", "requisito_cuenta_id")
    op.alter_column(
        "documentos_cuenta_cobro",
        "requisito_codigo",
        existing_type=sa.String(length=50),
        nullable=False,
    )

    op.drop_index("ix_requisitos_cuenta_cuenta_cobro_id", table_name="requisitos_cuenta")
    op.drop_table("requisitos_cuenta")
