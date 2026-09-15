"""feat(paquete): add paquete_job table

Adds `paquete_job` (radicacion-sin-friccion, Phase 2 slice 2.7): ONE row per
`cuenta_cobro_id` (unique constraint) tracking the background package-
generation job's state — `status` (pending/running/done/failed), the full
result shape (`storage_key`, `filename`, `size_bytes`, `listo_para_radicar`,
`pendientes`, `es_borrador`, `advertencias_coherencia` JSON) once `done`, and
`error`/`error_code` once `failed`. Mirrors `031_clasificacion_job` exactly in
shape/intent — see `app.models.paquete_job`'s module docstring.

New-table-only migration: does NOT alter any column on `cuentas_cobro` or any
other existing table — safe under local SQLite `create_all` and mirrored here
for Postgres.

Revision ID: 042_paquete_job
Revises: 041_cdp_enum_uppercase
Create Date: 2026-09-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "042_paquete_job"
down_revision = "041_cdp_enum_uppercase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paquete_job",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cuenta_cobro_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("storage_key", sa.String(length=500), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("listo_para_radicar", sa.Boolean(), nullable=True),
        sa.Column("pendientes", sa.Integer(), nullable=True),
        sa.Column("es_borrador", sa.Boolean(), nullable=True),
        sa.Column("advertencias_coherencia", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["cuenta_cobro_id"], ["cuentas_cobro.id"], name="fk_paquete_job_cuenta_cobro_id"),
        sa.UniqueConstraint("cuenta_cobro_id", name="uq_paquete_job_cuenta"),
    )
    op.create_index("ix_paquete_job_cuenta_cobro_id", "paquete_job", ["cuenta_cobro_id"])


def downgrade() -> None:
    op.drop_index("ix_paquete_job_cuenta_cobro_id", table_name="paquete_job")
    op.drop_table("paquete_job")
