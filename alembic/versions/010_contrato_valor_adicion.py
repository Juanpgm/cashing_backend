"""feat: add valor_adicion column to contratos table

Revision ID: 010_contrato_valor_adicion
Revises: 009_preferencias_usuario
Create Date: 2026-05-09
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "010_contrato_valor_adicion"
down_revision = "009_preferencias_usuario"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contratos",
        sa.Column(
            "valor_adicion",
            sa.Numeric(precision=15, scale=2),
            nullable=True,
            comment="Suma acumulada de adiciones y modificaciones al contrato (SECOP valor_adicion)",
        ),
    )


def downgrade() -> None:
    op.drop_column("contratos", "valor_adicion")
