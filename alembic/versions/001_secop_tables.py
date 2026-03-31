"""feat: add secop contratos procesos documentos cache tables

Revision ID: 001_secop_tables
Revises:
Create Date: 2026-03-31
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "001_secop_tables"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "secop_contratos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("id_contrato_secop", sa.String(200), nullable=False),
        sa.Column("cedula_contratista", sa.String(30), nullable=False),
        sa.Column("tipodocproveedor", sa.String(50), nullable=True),
        sa.Column("nombre_contratista", sa.String(500), nullable=True),
        sa.Column("nombre_entidad", sa.String(500), nullable=True),
        sa.Column("nit_entidad", sa.String(50), nullable=True),
        sa.Column("sector", sa.String(200), nullable=True),
        sa.Column("departamento", sa.String(100), nullable=True),
        sa.Column("ciudad", sa.String(100), nullable=True),
        sa.Column("proceso_de_compra", sa.String(200), nullable=True),
        sa.Column("numero_contrato", sa.String(200), nullable=True),
        sa.Column("referencia_del_contrato", sa.String(500), nullable=True),
        sa.Column("tipo_de_contrato", sa.String(200), nullable=True),
        sa.Column("modalidad_de_contratacion", sa.String(200), nullable=True),
        sa.Column("descripcion_del_proceso", sa.Text(), nullable=True),
        sa.Column("estado_contrato", sa.String(100), nullable=True),
        sa.Column("fecha_de_firma", sa.Date(), nullable=True),
        sa.Column("fecha_inicio", sa.Date(), nullable=True),
        sa.Column("fecha_fin", sa.Date(), nullable=True),
        sa.Column("valor_del_contrato", sa.Numeric(20, 2), nullable=True),
        sa.Column("valor_pagado", sa.Numeric(20, 2), nullable=True),
        sa.Column("datos_raw", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id_contrato_secop"),
    )
    op.create_index("ix_secop_contratos_cedula", "secop_contratos", ["cedula_contratista"])
    op.create_index("ix_secop_contratos_proceso", "secop_contratos", ["proceso_de_compra"])
    op.create_index("ix_secop_contratos_numero", "secop_contratos", ["numero_contrato"])

    op.create_table(
        "secop_procesos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("id_proceso_secop", sa.String(200), nullable=False),
        sa.Column("referencia_del_proceso", sa.String(500), nullable=True),
        sa.Column("nombre_del_procedimiento", sa.String(500), nullable=True),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("entidad", sa.String(500), nullable=True),
        sa.Column("nit_entidad", sa.String(50), nullable=True),
        sa.Column("departamento_entidad", sa.String(100), nullable=True),
        sa.Column("ciudad_entidad", sa.String(100), nullable=True),
        sa.Column("fase", sa.String(100), nullable=True),
        sa.Column("modalidad_de_contratacion", sa.String(200), nullable=True),
        sa.Column("precio_base", sa.Numeric(20, 2), nullable=True),
        sa.Column("estado_del_procedimiento", sa.String(100), nullable=True),
        sa.Column("fecha_de_publicacion", sa.Date(), nullable=True),
        sa.Column("adjudicado", sa.String(10), nullable=True),
        sa.Column("duracion", sa.String(50), nullable=True),
        sa.Column("unidad_de_duracion", sa.String(50), nullable=True),
        sa.Column("datos_raw", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id_proceso_secop"),
    )
    op.create_index("ix_secop_procesos_id", "secop_procesos", ["id_proceso_secop"])
    op.create_index("ix_secop_procesos_nit", "secop_procesos", ["nit_entidad"])

    op.create_table(
        "secop_documentos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("id_documento_secop", sa.String(200), nullable=False),
        sa.Column("numero_contrato", sa.String(200), nullable=True),
        sa.Column("proceso", sa.String(200), nullable=True),
        sa.Column("nombre_archivo", sa.String(500), nullable=True),
        sa.Column("tamanno_archivo", sa.String(50), nullable=True),
        sa.Column("extension", sa.String(20), nullable=True),
        sa.Column("descripcion", sa.String(500), nullable=True),
        sa.Column("fecha_carga", sa.Date(), nullable=True),
        sa.Column("entidad", sa.String(500), nullable=True),
        sa.Column("nit_entidad", sa.String(50), nullable=True),
        sa.Column("url_descarga", sa.String(1000), nullable=True),
        sa.Column("datos_raw", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id_documento_secop"),
    )
    op.create_index("ix_secop_docs_numero", "secop_documentos", ["numero_contrato"])
    op.create_index("ix_secop_docs_proceso", "secop_documentos", ["proceso"])


def downgrade() -> None:
    op.drop_table("secop_documentos")
    op.drop_table("secop_procesos")
    op.drop_table("secop_contratos")
