"""fix(cuenta-cobro): partial unique index on (contrato_id, numero_cuota)

Closes the check-then-insert race on `numero_cuota` (stepper step 2 now accepts
an explicit number): two concurrent creates could both pass the service's SELECT
check and insert duplicate numbers. The index predicate matches EXACTLY the
service's notion of "active" — `_numero_cuota_siguiente` and the explicit-numero
duplicate check both filter only on `deleted_at IS NULL` (there is no "anulada"
estado in `EstadoCuentaCobro`), so numbers freed by soft-deleted cuotas stay
reusable. NULL `numero_cuota` rows never collide (unique indexes treat NULLs as
distinct on both Postgres and SQLite).

Safe on legacy data: migration 025's backfill assigned sequential (unique)
numbers per contrato, and soft-deleted rows are outside the predicate.

Revision ID: 035_uq_contrato_numero_cuota
Revises: 024_integraciones_table
Create Date: 2026-08-04

Rechained 2026-08-11: `024_integraciones_table` also revised 034 (same parent),
leaving two heads and breaking `alembic upgrade head` at container startup. The
`integraciones` table already exists in the live DB with version_num=035, so the
linear order 034 -> 024_integraciones_table -> 035 matches applied reality.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "035_uq_contrato_numero_cuota"
down_revision = "024_integraciones_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_contrato_numero_cuota_activa",
        "cuentas_cobro",
        ["contrato_id", "numero_cuota"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
        sqlite_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_contrato_numero_cuota_activa", table_name="cuentas_cobro")
