"""feat(contratos): add location and cargo_supervisor columns to contratos table

Revision ID: 015_contrato_ubicacion
Revises: 014_photo_url_text
Create Date: 2026-06-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "015_contrato_ubicacion"
down_revision: str = "014_photo_url_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contratos", sa.Column("pais", sa.String(100), nullable=True))
    op.add_column("contratos", sa.Column("departamento", sa.String(100), nullable=True))
    op.add_column("contratos", sa.Column("ciudad", sa.String(100), nullable=True))
    op.add_column("contratos", sa.Column("direccion_ejecucion", sa.String(255), nullable=True))
    op.add_column("contratos", sa.Column("cargo_supervisor", sa.String(255), nullable=True))


def downgrade() -> None:
    op.drop_column("contratos", "cargo_supervisor")
    op.drop_column("contratos", "direccion_ejecucion")
    op.drop_column("contratos", "ciudad")
    op.drop_column("contratos", "departamento")
    op.drop_column("contratos", "pais")
