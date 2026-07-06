"""feat: enable pgvector extension and add embedding column to obligaciones

Revision ID: 008_pgvector_embeddings
Revises: 007_agent_runs_borradores
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008_pgvector_embeddings"
down_revision = "007_agent_runs_borradores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Enable pgvector extension
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Add embedding column (1536 dims — text-embedding-004)
    op.add_column(
        "obligaciones",
        sa.Column(
            "embedding",
            sa.Text(),
            nullable=True,
            comment="pgvector 1536-dim embedding encoded as text for portability",
        ),
    )

    # Add embedding to actividades for future semantic search
    op.add_column(
        "actividades",
        sa.Column(
            "embedding",
            sa.Text(),
            nullable=True,
            comment="pgvector 1536-dim embedding for activity similarity search",
        ),
    )

    # Create ivfflat index — partial (only non-null rows)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_indexes WHERE indexname = 'ix_obligaciones_embedding'
            ) THEN
                CREATE INDEX ix_obligaciones_embedding
                ON obligaciones USING ivfflat (
                    (embedding::vector(1536)) vector_cosine_ops
                ) WITH (lists = 100)
                WHERE embedding IS NOT NULL;
            END IF;
        END$$;
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_obligaciones_embedding")
    op.drop_column("actividades", "embedding")
    op.drop_column("obligaciones", "embedding")
    # Note: we intentionally do NOT drop the vector extension as other tables might use it
