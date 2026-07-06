"""feat(agent): AgentCheckpoint table for HIL pause state

Revision ID: 012_agent_checkpoints
Revises: 011_requisitos_documentos
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "012_agent_checkpoints"
down_revision: str = "011_requisitos_documentos"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_checkpoints",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("conversaciones.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("paused_node", sa.String(100), nullable=True),
        sa.Column(
            "estado",
            sa.String(20),
            server_default="completado",
            nullable=False,
        ),
        sa.Column(
            "state_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_agent_checkpoints_session_id",
        "agent_checkpoints",
        ["session_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_checkpoints_session_id", table_name="agent_checkpoints")
    op.drop_table("agent_checkpoints")
