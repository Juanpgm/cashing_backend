"""feat: checklist de documentos por cuenta de cobro (Phase 011)

Revision ID: 011_requisitos_documentos
Revises: 010_contrato_valor_adicion
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "011_requisitos_documentos"
down_revision = "010_contrato_valor_adicion"
branch_labels = None
depends_on = None


# Extra values added to the existing tipo_documento_fuente enum.
NEW_TIPO_VALUES = (
    "rpc",
    "seguridad_social",
    "comprobante_pago_ss",
    "informe_actividades",
    "informe_supervision",
    "ds_consecutivo",
    "cedula",
    "rut",
    "ficha_tecnica",
    "acta_inicio",
    "dependientes",
)


# Catalog seed — recurring requirements (every cuenta de cobro).
RECURRING = [
    {
        "codigo": "CONTRATO",
        "etiqueta": "Contrato / minuta / clausulado",
        "descripcion": "Documento del contrato firmado con las obligaciones del contratista.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "contrato",
        "keywords_deteccion": ["contrato", "minuta", "clausulado"],
        "orden": 10,
    },
    {
        "codigo": "RPC",
        "etiqueta": "Registro Presupuestal (RPC/RP)",
        "descripcion": "Registro Presupuestal del Compromiso emitido por hacienda/tesorería.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "rpc",
        "keywords_deteccion": ["rpc", "registro presupuestal", "rp ", "compromiso presupuestal"],
        "orden": 20,
    },
    {
        "codigo": "SEGURIDAD_SOCIAL",
        "etiqueta": "Planilla de aportes a seguridad social",
        "descripcion": "Soporte de pago de la planilla de seguridad social del periodo.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "seguridad_social",
        "keywords_deteccion": ["seguridad social", "planilla", "pila"],
        "orden": 30,
    },
    {
        "codigo": "COMPROBANTE_PAGO_SS",
        "etiqueta": "Comprobante de pago seguridad social (CUS)",
        "descripcion": "Comprobante de la transacción de pago / CUS.",
        "obligatorio": False,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "comprobante_pago_ss",
        "keywords_deteccion": ["comprobante pago", "cus", "comprobante seguridad social"],
        "orden": 40,
    },
    {
        "codigo": "INFORME_ACTIVIDADES",
        "etiqueta": "Informe de actividades",
        "descripcion": "Informe del contratista sobre las actividades realizadas (primera persona).",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": True,
        "tipo_documento_fuente": "informe_actividades",
        "keywords_deteccion": ["informe de actividades", "informe actividades"],
        "orden": 50,
    },
    {
        "codigo": "INFORME_SUPERVISION",
        "etiqueta": "Informe de supervisión",
        "descripcion": "Informe del supervisor sobre las actividades del contratista (tercera persona).",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": True,
        "tipo_documento_fuente": "informe_supervision",
        "keywords_deteccion": ["informe de supervision", "informe supervision", "supervisor"],
        "orden": 60,
    },
    {
        "codigo": "DS_CONSECUTIVO",
        "etiqueta": "DS / Consecutivo de pago",
        "descripcion": "Consecutivo de pago de la entidad territorial (opcional).",
        "obligatorio": False,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "ds_consecutivo",
        "keywords_deteccion": ["ds-", "consecutivo"],
        "orden": 70,
    },
    {
        "codigo": "EVIDENCIAS",
        "etiqueta": "Evidencias por obligación",
        "descripcion": "Soportes (documentos, fotos, actas, entregables) de cada obligación contractual.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": None,  # uses Evidencia model, not DocumentoFuente
        "keywords_deteccion": [],
        "orden": 80,
    },
]

# Only required on the first cuenta de cobro of the contract.
FIRST_ONLY = [
    {
        "codigo": "CEDULA",
        "etiqueta": "Cédula de ciudadanía",
        "descripcion": "Copia de la cédula del contratista.",
        "obligatorio": True,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "cedula",
        "keywords_deteccion": ["cedula", "cédula"],
        "orden": 110,
    },
    {
        "codigo": "RUT",
        "etiqueta": "RUT",
        "descripcion": "Registro Único Tributario actualizado.",
        "obligatorio": True,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "rut",
        "keywords_deteccion": ["rut"],
        "orden": 120,
    },
    {
        "codigo": "FICHA_TECNICA",
        "etiqueta": "Ficha técnica",
        "descripcion": "Ficha técnica del contratista.",
        "obligatorio": False,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "ficha_tecnica",
        "keywords_deteccion": ["ficha tecnica", "ficha técnica"],
        "orden": 130,
    },
    {
        "codigo": "ACTA_INICIO",
        "etiqueta": "Acta de inicio",
        "descripcion": "Acta de inicio del contrato firmada.",
        "obligatorio": True,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "acta_inicio",
        "keywords_deteccion": ["acta de inicio", "acta inicio"],
        "orden": 140,
    },
    {
        "codigo": "DEPENDIENTES",
        "etiqueta": "Certificado de dependientes",
        "descripcion": "Declaración de dependientes económicos.",
        "obligatorio": False,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "dependientes",
        "keywords_deteccion": ["dependientes"],
        "orden": 150,
    },
]


def upgrade() -> None:
    # ── Extend tipo_documento_fuente enum (Postgres) ────────────────────────
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for value in NEW_TIPO_VALUES:
            op.execute(
                f"ALTER TYPE tipo_documento_fuente ADD VALUE IF NOT EXISTS '{value}'"
            )

    # ── requisitos_documento (catalog) ──────────────────────────────────────
    op.create_table(
        "requisitos_documento",
        sa.Column("codigo", sa.String(length=50), nullable=False),
        sa.Column("etiqueta", sa.String(length=200), nullable=False),
        sa.Column("descripcion", sa.Text(), nullable=True),
        sa.Column("obligatorio", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "solo_primera_cuenta", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "permite_autogen", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("tipo_documento_fuente", sa.String(length=50), nullable=True),
        sa.Column("keywords_deteccion", sa.JSON(), nullable=False),
        sa.Column("orden", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("codigo"),
    )

    # ── documentos_cuenta_cobro (link/state per requirement) ────────────────
    op.create_table(
        "documentos_cuenta_cobro",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cuenta_cobro_id", sa.Uuid(), nullable=False),
        sa.Column("requisito_codigo", sa.String(length=50), nullable=False),
        sa.Column(
            "estado",
            sa.Enum(
                "pendiente",
                "detectado",
                "cargado",
                "cumplido_manual",
                "no_aplica",
                name="estado_requisito_documento",
            ),
            nullable=False,
            server_default="pendiente",
        ),
        sa.Column("documento_fuente_id", sa.Uuid(), nullable=True),
        sa.Column("secop_documento_id", sa.Uuid(), nullable=True),
        sa.Column("confianza_deteccion", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("observaciones", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cuenta_cobro_id"], ["cuentas_cobro.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requisito_codigo"], ["requisitos_documento.codigo"]
        ),
        sa.ForeignKeyConstraint(
            ["documento_fuente_id"], ["documentos_fuente.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["secop_documento_id"], ["secop_documentos.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cuenta_cobro_id", "requisito_codigo", name="uq_docccobro_cuenta_requisito"
        ),
    )
    op.create_index(
        "ix_documentos_cuenta_cobro_cuenta_cobro_id",
        "documentos_cuenta_cobro",
        ["cuenta_cobro_id"],
    )
    op.create_index(
        "ix_documentos_cuenta_cobro_requisito_codigo",
        "documentos_cuenta_cobro",
        ["requisito_codigo"],
    )

    # ── documento_checklist_candidatos (SECOP top-N matches) ────────────────
    op.create_table(
        "documento_checklist_candidatos",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cuenta_cobro_id", sa.Uuid(), nullable=False),
        sa.Column("requisito_codigo", sa.String(length=50), nullable=False),
        sa.Column("secop_documento_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cuenta_cobro_id"], ["cuentas_cobro.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["requisito_codigo"], ["requisitos_documento.codigo"]
        ),
        sa.ForeignKeyConstraint(
            ["secop_documento_id"], ["secop_documentos.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cuenta_cobro_id",
            "requisito_codigo",
            "secop_documento_id",
            name="uq_doccand_cuenta_req_secop",
        ),
    )
    op.create_index(
        "ix_documento_checklist_candidatos_cuenta_cobro_id",
        "documento_checklist_candidatos",
        ["cuenta_cobro_id"],
    )
    op.create_index(
        "ix_documento_checklist_candidatos_requisito_codigo",
        "documento_checklist_candidatos",
        ["requisito_codigo"],
    )

    # ── Seed catalog ────────────────────────────────────────────────────────
    requisitos_table = sa.table(
        "requisitos_documento",
        sa.column("codigo", sa.String),
        sa.column("etiqueta", sa.String),
        sa.column("descripcion", sa.Text),
        sa.column("obligatorio", sa.Boolean),
        sa.column("solo_primera_cuenta", sa.Boolean),
        sa.column("permite_autogen", sa.Boolean),
        sa.column("tipo_documento_fuente", sa.String),
        sa.column("keywords_deteccion", sa.JSON),
        sa.column("orden", sa.Integer),
    )
    op.bulk_insert(requisitos_table, RECURRING + FIRST_ONLY)


def downgrade() -> None:
    op.drop_index(
        "ix_documento_checklist_candidatos_requisito_codigo",
        table_name="documento_checklist_candidatos",
    )
    op.drop_index(
        "ix_documento_checklist_candidatos_cuenta_cobro_id",
        table_name="documento_checklist_candidatos",
    )
    op.drop_table("documento_checklist_candidatos")
    op.drop_index(
        "ix_documentos_cuenta_cobro_requisito_codigo",
        table_name="documentos_cuenta_cobro",
    )
    op.drop_index(
        "ix_documentos_cuenta_cobro_cuenta_cobro_id",
        table_name="documentos_cuenta_cobro",
    )
    op.drop_table("documentos_cuenta_cobro")
    op.drop_table("requisitos_documento")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="estado_requisito_documento").drop(bind, checkfirst=True)
    # Note: tipo_documento_fuente enum values are not removed (Postgres limitation).
