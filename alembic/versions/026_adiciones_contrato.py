"""feat(adiciones): add adiciones_contrato table and una_vez flag

Adds the contract Adición event log (billing-resilience-templates, slice #4):
`adiciones_contrato` (tipo adicion/prorroga/otrosi, numero, rpc_nuevo, cdp_nuevo,
valor_adicion, nueva_fecha_fin, descripcion, fecha_evento) and `Obligacion.una_vez`
(one-time obligations blank after cuota 1, wired in slice #6).

`Contrato.valor_adicion` (migration 010) is kept for back-compat (design D4) — this
migration does not touch it; events in the new table become the authoritative source
for cuota position/final detection and the coherence validator's R6 rule.

`op.add_column` for `una_vez` is explicit (not `create_all`) for the same reason as
migration 025: `Base.metadata.create_all` (used by the SQLite test DB) silently
no-ops column additions to an EXISTING table — must be applied explicitly on Neon
(see task 4.5).

Revision ID: 026_adiciones_contrato
Revises: 025_cuota_position
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026_adiciones_contrato"
down_revision = "025_cuota_position"
branch_labels = None
depends_on = None

_TIPO_VALUES = ("adicion", "prorroga", "otrosi")
tipo_adicion_enum = sa.Enum(*_TIPO_VALUES, name="tipo_adicion")


def upgrade() -> None:
    bind = op.get_bind()
    tipo_adicion_enum.create(bind, checkfirst=True)

    op.create_table(
        "adiciones_contrato",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("contrato_id", sa.Uuid(), nullable=False),
        sa.Column("tipo", sa.Enum(*_TIPO_VALUES, name="tipo_adicion", create_type=False), nullable=False),
        sa.Column("numero", sa.Integer(), nullable=False),
        sa.Column("rpc_nuevo", sa.String(length=50), nullable=True),
        sa.Column("cdp_nuevo", sa.String(length=50), nullable=True),
        sa.Column("valor_adicion", sa.Numeric(15, 2), nullable=True),
        sa.Column("nueva_fecha_fin", sa.Date(), nullable=True),
        sa.Column("descripcion", sa.Text(), nullable=False, server_default=""),
        sa.Column("fecha_evento", sa.Date(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["contrato_id"], ["contratos.id"], name="fk_adiciones_contrato_contrato_id"),
    )
    op.create_index("ix_adiciones_contrato_contrato_id", "adiciones_contrato", ["contrato_id"])

    op.add_column(
        "obligaciones",
        sa.Column("una_vez", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("obligaciones", "una_vez")
    op.drop_index("ix_adiciones_contrato_contrato_id", table_name="adiciones_contrato")
    op.drop_table("adiciones_contrato")
    tipo_adicion_enum.drop(op.get_bind(), checkfirst=True)
