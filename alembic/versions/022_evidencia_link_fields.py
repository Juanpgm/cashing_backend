"""feat: link-type evidencias (fuente, url)

Adds ``fuente`` and ``url`` to ``evidencias`` so an evidence row can point to an
external link (Gmail message, Drive file, Calendar event) discovered by the
evidence-discovery agent, instead of only referencing an uploaded file.

Also relaxes ``storage_key``, ``tipo_archivo`` and ``tamano_bytes`` to nullable:
a row is now EITHER a stored file (``storage_key`` set) OR an external link
(``url`` set), never both.

Revision ID: 022_evidencia_link_fields
Revises: 021_invite_codes
Create Date: 2026-07-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "022_evidencia_link_fields"
down_revision = "021_invite_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("evidencias", schema=None) as batch_op:
        batch_op.add_column(sa.Column("fuente", sa.String(length=50), nullable=True))
        batch_op.add_column(sa.Column("url", sa.String(length=1000), nullable=True))
        batch_op.alter_column("storage_key", existing_type=sa.String(length=500), nullable=True)
        batch_op.alter_column("tipo_archivo", existing_type=sa.String(length=100), nullable=True)
        batch_op.alter_column("tamano_bytes", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    # DESTRUCTIVE: link-type evidencia rows (storage_key IS NULL — external Gmail/
    # Drive/Calendar links, no uploaded file) cannot be represented once
    # storage_key/tipo_archivo/tamano_bytes go back to NOT NULL below, so they are
    # deleted first. This intentionally loses data for any evidence discovered
    # after this migration's upgrade(); it is the only way to make the downgrade
    # succeed against a database that already has link-type rows.
    op.execute("DELETE FROM evidencias WHERE storage_key IS NULL")

    with op.batch_alter_table("evidencias", schema=None) as batch_op:
        batch_op.alter_column("tamano_bytes", existing_type=sa.BigInteger(), nullable=False)
        batch_op.alter_column("tipo_archivo", existing_type=sa.String(length=100), nullable=False)
        batch_op.alter_column("storage_key", existing_type=sa.String(length=500), nullable=False)
        batch_op.drop_column("url")
        batch_op.drop_column("fuente")
