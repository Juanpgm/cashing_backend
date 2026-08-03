"""feat(cuenta-cobro): add nullable consecutivo_ds column

Adds `consecutivo_ds` (nullable `VARCHAR(50)`) to `cuentas_cobro` (G4). Real SYJ
packages radicate a "DS-4161-<consecutivo>" xlsx whose consecutive changes per
cuota (113 -> 788 -> 1768 -> ...); this column captures it. Additive and
non-breaking: existing rows load with NULL. Set via the same stepper PATCH used
for `fecha_transaccion`, consumed by `informe_service._valores_plantilla` as
`cuota.consecutivo_ds` — omitted from the filled Documento Soporte template
when null (existing blank-field behavior).

No change to credit charging, cuota-position guards, or any other hardened gate.

Revision ID: 034_cuenta_cobro_consecutivo_ds
Revises: 033_sniff_contenido
Create Date: 2026-08-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "034_cuenta_cobro_consecutivo_ds"
down_revision = "033_sniff_contenido"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cuentas_cobro", sa.Column("consecutivo_ds", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("cuentas_cobro", "consecutivo_ds")
