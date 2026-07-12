"""feat(cuota-position): add numero_cuota/posicion/informe_final + backfill

Adds the stored cuota position model to `cuentas_cobro` (billing-resilience-templates,
slice #3): `numero_cuota` (1-based ordinal, nullable), `posicion`
(primera/recurrente/final, default `recurrente`), `informe_final` (explicit flag,
default `false`). Replaces the previous read-time `checklist_service._is_first_cuenta`
positional heuristic and the coherence validator's interim chronological derivation.

Explicit `op.add_column` x3 — `Base.metadata.create_all` (used by the SQLite test DB)
silently no-ops column additions to existing tables, so this must be applied
explicitly on Neon (see task 3.5).

Backfill: assigns `numero_cuota`/`posicion` to every pre-existing row, per contrato,
in chronological (anio, mes) order (ties broken by `id` for determinism). Only
`primera`/`recurrente` are ever backfilled — `final`/`informe_final=true` are never
inferred (design decision D3); a human must set the final cuota explicitly.

Revision ID: 025_cuota_position
Revises: 023_documento_requisito_vinculos
Create Date: 2026-07-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025_cuota_position"
down_revision = "023_documento_requisito_vinculos"
branch_labels = None
depends_on = None

_POSICION_VALUES = ("primera", "recurrente", "final")
posicion_enum = sa.Enum(*_POSICION_VALUES, name="posicion_cuota")


def upgrade() -> None:
    bind = op.get_bind()
    posicion_enum.create(bind, checkfirst=True)

    op.add_column("cuentas_cobro", sa.Column("numero_cuota", sa.Integer(), nullable=True))
    op.add_column(
        "cuentas_cobro",
        sa.Column(
            "posicion",
            sa.Enum(*_POSICION_VALUES, name="posicion_cuota", create_type=False),
            nullable=False,
            server_default="recurrente",
        ),
    )
    op.add_column(
        "cuentas_cobro",
        sa.Column("informe_final", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # ── Backfill legacy rows ────────────────────────────────────────────────
    # Every pre-existing cuota (including soft-deleted ones — the requirement is
    # "no cuota is left without a position", not just active ones) gets a
    # numero_cuota/posicion consistent with its historical chronological order
    # within its own contrato.
    rows = bind.execute(
        sa.text("SELECT id, contrato_id FROM cuentas_cobro ORDER BY contrato_id, anio ASC, mes ASC, id ASC")
    ).fetchall()

    contador: dict[object, int] = {}
    for row in rows:
        contrato_id = row.contrato_id
        numero = contador.get(contrato_id, 0) + 1
        contador[contrato_id] = numero
        posicion = "primera" if numero == 1 else "recurrente"
        bind.execute(
            sa.text("UPDATE cuentas_cobro SET numero_cuota = :numero, posicion = :posicion WHERE id = :id"),
            {"numero": numero, "posicion": posicion, "id": row.id},
        )


def downgrade() -> None:
    op.drop_column("cuentas_cobro", "informe_final")
    op.drop_column("cuentas_cobro", "posicion")
    op.drop_column("cuentas_cobro", "numero_cuota")
    posicion_enum.drop(op.get_bind(), checkfirst=True)
