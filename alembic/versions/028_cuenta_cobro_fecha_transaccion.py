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
    # `alembic_version.version_num` defaults to VARCHAR(32) — this revision's own
    # id is 34 chars, so on any database whose alembic_version table was created
    # with the stock width (never widened before now), the version bump that
    # Alembic runs right after this function returns would fail with "value too
    # long for type character varying(32)". Widen it first, once, here — the
    # earliest point in the chain this project has organically reached where a
    # too-long id is about to be written (005/018/020 are longer still but sit
    # before this environment's starting point and were never actually replayed
    # through Alembic — see the alembic-chain-broken finding in project memory).
    # Safe/idempotent: widening a varchar never truncates existing data, and this
    # runs regardless of dialect (SQLite ignores column length constraints, so it
    # only matters for Postgres — but this migration only touches Postgres real
    # environments in practice, and running it there is what fixes them).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(64)")

    op.add_column("cuentas_cobro", sa.Column("fecha_transaccion", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("cuentas_cobro", "fecha_transaccion")
