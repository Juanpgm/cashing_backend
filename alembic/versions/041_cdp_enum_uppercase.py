"""feat(documentos): add uppercase 'CDP' label to tipo_documento_fuente enum

Same gap as 040_tipo_documento_fuente_otros, this time for the ``CDP`` member.
037_tipo_documento_fuente_cdp only added the lowercase label ``'cdp'``, but
``DocumentoFuente.tipo`` is mapped as
``Enum(TipoDocumentoFuente, name="tipo_documento_fuente")`` WITHOUT
``values_callable`` — SQLAlchemy's default, which persists the member NAME, not
its value. So ``TipoDocumentoFuente.CDP`` actually gets sent to Postgres as
``'CDP'`` (uppercase), a label the type never had. Uploading a CDP document
would hit the same enum-mismatch failure that 040 fixed for ``OTROS``.

``'cdp'`` (lowercase, added by 037) stays in the type too — inert, but kept for
consistency with 011/037's existing lowercase labels, same reasoning as 040.

Postgres requires that a value added with ALTER TYPE ... ADD VALUE is not used
in the same transaction, so this revision carries no other DDL/DML — same
isolation as 032_categoria_cdp, 037_tipo_documento_fuente_cdp and
040_tipo_documento_fuente_otros.

SQLite (local dev / tests) stores this enum as VARCHAR without a CHECK
constraint (SQLAlchemy 2.x default create_constraint=False), so the dialect
guard makes this a no-op there.

Revision ID: 041_cdp_enum_uppercase
Revises: 040_tipo_documento_fuente_otros
Create Date: 2026-09-04
"""

from __future__ import annotations

from alembic import op

revision = "041_cdp_enum_uppercase"
down_revision = "040_tipo_documento_fuente_otros"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Uppercase: this is the label SQLAlchemy actually emits for
        # TipoDocumentoFuente.CDP with the current mapping (no values_callable).
        op.execute("ALTER TYPE tipo_documento_fuente ADD VALUE IF NOT EXISTS 'CDP'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type; the added value is
    # inert if unused. Intentional no-op.
    pass
