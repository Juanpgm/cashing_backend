"""feat(documentos): add persistent categoria column to secop_documentos and documentos_fuente

Revision ID: 017_categoria_documento
Revises: 016_obligacion_etiqueta
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "017_categoria_documento"
down_revision = "016_obligacion_etiqueta"
branch_labels = None
depends_on = None

CATEGORIA_ENUM_VALUES = (
    "contrato",
    "registro_presupuestal",
    "acta_inicio",
    "rut",
    "cedula",
    "seguridad_social",
    "evidencias",
    "otros",
)

categoria_enum = sa.Enum(*CATEGORIA_ENUM_VALUES, name="categoria_documento")


def upgrade() -> None:
    categoria_enum.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "secop_documentos",
        sa.Column(
            "categoria",
            sa.Enum(*CATEGORIA_ENUM_VALUES, name="categoria_documento", create_type=False),
            nullable=False,
            server_default="otros",
        ),
    )
    op.add_column(
        "secop_documentos",
        sa.Column("categoria_confianza", sa.Numeric(4, 3), nullable=True),
    )
    op.add_column(
        "secop_documentos",
        sa.Column("categoria_override", sa.Boolean(), nullable=False, server_default="false"),
    )

    op.add_column(
        "documentos_fuente",
        sa.Column(
            "categoria",
            sa.Enum(*CATEGORIA_ENUM_VALUES, name="categoria_documento", create_type=False),
            nullable=False,
            server_default="otros",
        ),
    )
    op.add_column(
        "documentos_fuente",
        sa.Column("categoria_confianza", sa.Numeric(4, 3), nullable=True),
    )
    op.add_column(
        "documentos_fuente",
        sa.Column("categoria_override", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("documentos_fuente", "categoria_override")
    op.drop_column("documentos_fuente", "categoria_confianza")
    op.drop_column("documentos_fuente", "categoria")

    op.drop_column("secop_documentos", "categoria_override")
    op.drop_column("secop_documentos", "categoria_confianza")
    op.drop_column("secop_documentos", "categoria")

    categoria_enum.drop(op.get_bind(), checkfirst=True)
