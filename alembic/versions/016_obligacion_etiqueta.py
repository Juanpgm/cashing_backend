"""feat(obligaciones): add etiqueta column to preserve original contract marker

Revision ID: 016_obligacion_etiqueta
Revises: 015_contrato_ubicacion
Create Date: 2026-06-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "016_obligacion_etiqueta"
down_revision = "015_contrato_ubicacion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "obligaciones",
        sa.Column("etiqueta", sa.String(8), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("obligaciones", "etiqueta")
