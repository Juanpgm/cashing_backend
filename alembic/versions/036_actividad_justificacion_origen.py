"""feat(actividad): add justificacion_origen (llm/seed/manual/sin_labores)

Adds provenance tracking for `Actividad.justificacion` so DOCX rendering and
coverage checks (`_tiene_justificacion_real`) can distinguish real LLM/manual
text from the deterministic seed fallback and the "sin labores" tactful text.

Backfill: `server_default="seed"` applies to every pre-existing row, including
ones that already hold real LLM-authored text — those rows will read as
`seed` (BORRADOR / not-covered) until regenerated. Known, accepted tradeoff
(see workstream B plan) — not fixed here.

Revision ID: 036_actividad_justificacion_origen
Revises: 035_uq_contrato_numero_cuota
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "036_actividad_justificacion_origen"
down_revision = "035_uq_contrato_numero_cuota"
branch_labels = None
depends_on = None

_ORIGEN_VALUES = ("llm", "seed", "manual", "sin_labores")
origen_enum = sa.Enum(*_ORIGEN_VALUES, name="justificacion_origen")


def upgrade() -> None:
    bind = op.get_bind()
    origen_enum.create(bind, checkfirst=True)
    op.add_column(
        "actividades",
        sa.Column(
            "justificacion_origen",
            sa.Enum(*_ORIGEN_VALUES, name="justificacion_origen", create_type=False),
            nullable=False,
            server_default="seed",
        ),
    )


def downgrade() -> None:
    op.drop_column("actividades", "justificacion_origen")
    origen_enum.drop(op.get_bind(), checkfirst=True)
