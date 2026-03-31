"""feat: add documento_proveedor to contratos and truncate table

Revision ID: 002_contrato_documento_proveedor
Revises: 001_secop_tables
Create Date: 2026-03-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_contrato_documento_proveedor"
down_revision = "001_secop_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Clear all data (cascades to obligaciones via FK)
    op.execute("DELETE FROM obligaciones")
    op.execute("DELETE FROM actividades")
    op.execute("DELETE FROM cuentas_cobro")
    op.execute("DELETE FROM contratos")

    # Add new column
    op.add_column(
        "contratos",
        sa.Column("documento_proveedor", sa.String(30), nullable=True),
    )
    op.create_index("ix_contratos_documento_proveedor", "contratos", ["documento_proveedor"])


def downgrade() -> None:
    op.drop_index("ix_contratos_documento_proveedor", table_name="contratos")
    op.drop_column("contratos", "documento_proveedor")
