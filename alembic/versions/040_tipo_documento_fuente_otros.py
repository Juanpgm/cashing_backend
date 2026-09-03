"""feat(documentos): add neutral 'otros' value to tipo_documento_fuente enum

Requisitos that declare no ``tipo_documento_fuente`` — EVIDENCIAS and every
user-defined per-cuenta requisito — had no representable enum value. Clients
either sent ``otros`` (web) or the lowercased requisito code (mobile) and got a
422, or fell back to ``contrato``, which used to trigger the
1-document-per-contract replace rule and destroy the contract document.

CASING — both cases are added on purpose, because the repository's own evidence
is contradictory:

  * ``DocumentoFuente.tipo`` is mapped as
    ``Enum(TipoDocumentoFuente, name="tipo_documento_fuente")`` WITHOUT
    ``values_callable``. That is SQLAlchemy's default, which persists the member
    NAMES. Verified against the installed SQLAlchemy 2.0.51:
    ``DocumentoFuente.__table__.c.tipo.type.enums`` returns
    ``['CONTRATO', 'INSTRUCCIONES', ...]`` (uppercase) and
    ``_db_value_for_elem(TipoDocumentoFuente.CONTRATO)`` returns ``'CONTRATO'``.
    The sibling ``categoria`` column DOES pass ``values_callable`` and is
    lowercase — so the two columns genuinely differ.
  * Every previous enum migration (011 and 037) added LOWERCASE labels, which
    the ORM therefore never emits. Those labels are inert.

``'OTROS'`` (uppercase) is the label the ORM will actually send. ``'otros'`` is
added too so the type stays consistent with the labels 011/037 already created
and so any hand-written SQL or future ``values_callable`` switch keeps working.
An unused enum label costs nothing.

Postgres requires that a value added with ALTER TYPE ... ADD VALUE is not used
in the same transaction, so this revision carries no other DDL/DML — same
isolation as 032_categoria_cdp and 037_tipo_documento_fuente_cdp.

SQLite (local dev / tests) stores this enum as VARCHAR without a CHECK
constraint (SQLAlchemy 2.x default create_constraint=False), so the dialect
guard makes this a no-op there.

Revision ID: 040_tipo_documento_fuente_otros
Revises: 039_documento_fuente_sha256
Create Date: 2026-09-03
"""

from __future__ import annotations

from alembic import op

revision = "040_tipo_documento_fuente_otros"
down_revision = "039_documento_fuente_sha256"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Uppercase first: this is the label SQLAlchemy actually emits for
        # TipoDocumentoFuente.OTROS with the current mapping.
        op.execute("ALTER TYPE tipo_documento_fuente ADD VALUE IF NOT EXISTS 'OTROS'")
        op.execute("ALTER TYPE tipo_documento_fuente ADD VALUE IF NOT EXISTS 'otros'")


def downgrade() -> None:
    # PostgreSQL cannot remove a value from an enum type; the added value is
    # inert if unused. Intentional no-op.
    pass
