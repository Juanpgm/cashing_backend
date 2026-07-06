"""feat(auth): extend photo_url column to Text to support long presigned URLs

Revision ID: 014_photo_url_text
Revises: 013_google_auth_fields
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "014_photo_url_text"
down_revision: str = "013_google_auth_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "usuarios",
        "photo_url",
        type_=sa.Text(),
        existing_type=sa.String(500),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "usuarios",
        "photo_url",
        type_=sa.String(500),
        existing_type=sa.Text(),
        existing_nullable=True,
    )
