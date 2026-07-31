"""feat(evidencias-ai-classification): add clasificacion_evidencias_job table

Adds `clasificacion_evidencias_job` (evidence-classification-jobs, slice #5):
ONE row per `cuenta_cobro_id` (unique constraint) tracking the background
classification job's aggregate progress — `status` (pending/running/done/
failed), `total`, `procesadas`, and an optional `error` message. Per-file/
per-obligación detail is derived from the existing `evidencia_obligacion`
table at read time, not duplicated here.

New-table-only migration: does NOT alter any column on `cuentas_cobro` or any
other existing table — safe under local SQLite `create_all` and mirrored here
for Postgres.

Revision ID: 031_clasificacion_job
Revises: 030_evidencia_obligacion_link
Create Date: 2026-07-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "031_clasificacion_job"
down_revision = "030_evidencia_obligacion_link"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clasificacion_evidencias_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cuenta_cobro_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("procesadas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["cuenta_cobro_id"], ["cuentas_cobro.id"], name="fk_clasificacion_job_cuenta_cobro_id"),
        sa.UniqueConstraint("cuenta_cobro_id", name="uq_clasificacion_job_cuenta"),
    )
    op.create_index(
        "ix_clasificacion_evidencias_job_cuenta_cobro_id", "clasificacion_evidencias_job", ["cuenta_cobro_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_clasificacion_evidencias_job_cuenta_cobro_id", table_name="clasificacion_evidencias_job")
    op.drop_table("clasificacion_evidencias_job")
