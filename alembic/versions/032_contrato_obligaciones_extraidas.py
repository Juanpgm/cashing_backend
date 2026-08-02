"""feat(radicar-ui-ux-improvements): add obligaciones_extraidas to contratos

Additive nullable column on `contratos` (C.3): set at SECOP import time to
True/False whether obligaciones extraction (deterministic + LLM fallback)
found any obligaciones, so the frontend can distinguish "imported, none
extracted" from "user hasn't added any yet". None = manually created /
legacy contract (no backfill needed).

Revision ID: 032_contrato_obligaciones_extraidas
Revises: 031_clasificacion_job
Create Date: 2026-08-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "032_contrato_obligaciones_extraidas"
down_revision = "031_clasificacion_job"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contratos",
        sa.Column(
            "obligaciones_extraidas",
            sa.Boolean(),
            nullable=True,
            comment=(
                "Set at SECOP import time: True/False whether the deterministic+LLM-fallback "
                "extraction found any obligaciones. None = manually created / legacy contract."
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("contratos", "obligaciones_extraidas")
