"""feat(cuenta-cobro): add nullable fecha_transaccion column

Adds `fecha_transaccion` (nullable `DATE`) to `cuentas_cobro` (radicacion-stepper,
work unit B1). Additive and non-breaking: existing rows load with `NULL`. Set at
stepper step 2 via `CuentaCobroCreate.fecha_transaccion`, persisted by
`crear_cuenta_cobro`. Used as the bounding date for month-scoped pipelines
(evidence discovery, justification) when present; falls back to `mes`/`anio`
when absent (see spec "fecha_transaccion Column Semantics").

No change to credit charging, cuota-position guards, or any other hardened gate.

Revision ID: 028_cuenta_cobro_fecha_transaccion
Revises: 027_plantillas_organismo
Create Date: 2026-07-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "028_cuenta_cobro_fecha_transaccion"
down_revision = "027_plantillas_organismo"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cuentas_cobro", sa.Column("fecha_transaccion", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("cuentas_cobro", "fecha_transaccion")
