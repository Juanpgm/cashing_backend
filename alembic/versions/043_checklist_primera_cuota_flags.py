"""fix(checklist): RPC/CDP/CONTRATO become solo_primera_cuenta

checklist/primera-cuota-2026-09-16: CEDULA, RUT, RPC and CDP are now requested
ONLY on a contract's first cuota; CONTRATO is first-cuota-only too (but can
reappear on a later cuota — see `checklist_service.requisito_aplica_a_cuenta`).
`app.services.checklist_service._CATALOGO_SEED` already carries the new flags
for a FRESH deployment (empty table, full seed insert), but
`_seed_catalogo_si_vacio` only INSERTS missing codes into an already-seeded
table — it never UPDATEs an existing row's columns (see that function's
docstring: "No existing row is ever touched, updated, or removed"). Any
deployment whose `requisitos_documento` table was already populated (every
real environment, since this table has existed since migration 011) needs this
migration to actually flip RPC/CDP/CONTRATO for existing data; the code-side
seed change alone only affects a brand-new/test DB.

CEDULA and RUT are untouched — already `solo_primera_cuenta=True` since 011.

Revision ID: 043_checklist_primera_cuota_flags
Revises: 042_paquete_job
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "043_checklist_primera_cuota_flags"
down_revision = "042_paquete_job"
branch_labels = None
depends_on = None

_CODIGOS = ("RPC", "CDP", "CONTRATO")

requisitos_table = sa.table(
    "requisitos_documento",
    sa.column("codigo", sa.String),
    sa.column("solo_primera_cuenta", sa.Boolean),
)


def upgrade() -> None:
    op.execute(requisitos_table.update().where(requisitos_table.c.codigo.in_(_CODIGOS)).values(solo_primera_cuenta=True))


def downgrade() -> None:
    op.execute(
        requisitos_table.update().where(requisitos_table.c.codigo.in_(_CODIGOS)).values(solo_primera_cuenta=False)
    )
