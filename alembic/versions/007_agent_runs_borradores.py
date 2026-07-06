"""feat: create agent_runs and borradores_cuenta_cobro tables

Revision ID: 007_agent_runs_borradores
Revises: 006_google_tokens_table
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "007_agent_runs_borradores"
down_revision = "006_google_tokens_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- agent_runs -------------------------------------------------------
    # Tracks every LangGraph execution: tokens used, cost, duration, quality.
    op.create_table(
        "agent_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("usuario_id", sa.Uuid(), nullable=False),
        sa.Column(
            "conversacion_id",
            sa.Uuid(),
            nullable=True,
            comment="LangGraph thread_id — maps to conversaciones.id",
        ),
        sa.Column("modo", sa.String(length=50), nullable=False),
        sa.Column(
            "nodo_actual",
            sa.String(length=100),
            nullable=True,
            comment="Last node executed when run completed/failed",
        ),
        sa.Column(
            "estado",
            sa.String(length=20),
            nullable=False,
            server_default="en_progreso",
            comment="en_progreso | completado | fallido | pausado_hil",
        ),
        sa.Column("tokens_usados", sa.Integer(), nullable=True),
        sa.Column("costo_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("duracion_ms", sa.Integer(), nullable=True),
        sa.Column(
            "quality_score",
            sa.Numeric(4, 3),
            nullable=True,
            comment="0.0 – 1.0 from judge node",
        ),
        sa.Column("modelo_usado", sa.String(length=100), nullable=True),
        sa.Column("creditos_consumidos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_mensaje", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["usuario_id"], ["usuarios.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["conversacion_id"],
            ["conversaciones.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_agent_runs_usuario_id", "agent_runs", ["usuario_id"])
    op.create_index("ix_agent_runs_conversacion_id", "agent_runs", ["conversacion_id"])
    op.create_index("ix_agent_runs_estado", "agent_runs", ["estado"])
    op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])

    # --- borradores_cuenta_cobro ------------------------------------------
    # Versioned drafts of a cuenta de cobro enabling HIL review + diff.
    op.create_table(
        "borradores_cuenta_cobro",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cuenta_cobro_id", sa.Uuid(), nullable=False),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="Monotonically increasing draft version number",
        ),
        sa.Column(
            "contenido",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            comment="Full rendered content of this draft version",
        ),
        sa.Column(
            "diff",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
            comment="JSON diff against previous version (null for v1)",
        ),
        sa.Column(
            "feedback_usuario",
            sa.Text(),
            nullable=True,
            comment="User feedback used to generate next version",
        ),
        sa.Column(
            "aprobado",
            sa.Boolean(),
            nullable=False,
            server_default="false",
            comment="True when user approves this version for PDF generation",
        ),
        sa.Column(
            "aprobado_en",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cuenta_cobro_id"],
            ["cuentas_cobro.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cuenta_cobro_id",
            "version",
            name="uq_borradores_cuenta_cobro_version",
        ),
    )
    op.create_index(
        "ix_borradores_cuenta_cobro_id",
        "borradores_cuenta_cobro",
        ["cuenta_cobro_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_borradores_cuenta_cobro_id",
        table_name="borradores_cuenta_cobro",
    )
    op.drop_table("borradores_cuenta_cobro")

    op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")
    op.drop_index("ix_agent_runs_estado", table_name="agent_runs")
    op.drop_index("ix_agent_runs_conversacion_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_usuario_id", table_name="agent_runs")
    op.drop_table("agent_runs")
