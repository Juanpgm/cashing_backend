"""fix(documentos): add OTROS and CDP to the tipo_documento_fuente enum

`OTROS` is the neutral document type. Before it existed, `TipoDocumentoFuente`
had no member meaning "no specific kind", so callers uploading into a checklist
requisito with `tipo_documento_fuente = NULL` (EVIDENCIAS and every user-defined
per-cuenta requisito) had to invent a fallback. The invented fallback was
`contrato`, which triggered the 1-document-per-contract replace rule in
`document_service.upload_document` and deleted the user's contract document from
storage and from the database.

`CDP` closes a catalog/enum disagreement: the checklist catalog has seeded the
CDP requisito with `tipo_documento_fuente = "cdp"` since the radicacion stepper,
but `cdp` was never a member of the enum, so forwarding that value verbatim was
rejected. CDP is a real contract-level document (it is listed in
`checklist_service._NIVEL_CONTRATO` and has its own detection keywords), so the
enum is corrected to match the catalog rather than the catalog degraded to NULL.

Label casing — deliberate, both casings are added:
`documentos_fuente.tipo` maps `Enum(TipoDocumentoFuente, name="tipo_documento_fuente")`
WITHOUT `values_callable`, so SQLAlchemy emits the enum member NAMES ('OTROS',
'CDP') as the Postgres labels, both when creating the type via
`Base.metadata.create_all` and on every INSERT. Migration 011 nevertheless added
its new labels in lowercase ('rpc', 'seguridad_social', ...). Depending on how a
given database got its type, either casing may be the one in use, so this
migration adds both. `ADD VALUE IF NOT EXISTS` makes the redundant one a no-op.

Postgres only: SQLite (test DB) stores enums as VARCHAR, so there is no type to
alter. Postgres cannot remove an enum label, hence the no-op downgrade.

Revision ID: 029_tipo_doc_fuente_otros_cdp
Revises: 028_cuenta_cobro_fecha_transaccion
Create Date: 2026-09-02

The revision id is kept at 32 characters or fewer on purpose. Alembic's
`alembic_version.version_num` column defaults to VARCHAR(32) and `env.py` does
not override `version_table_column_length`, so a longer id fails to record with
"value too long for type character varying(32)". Revisions 005, 018, 020 and 028
already exceed that limit; do not add more.
"""

from __future__ import annotations

from alembic import op

revision = "029_tipo_doc_fuente_otros_cdp"
down_revision = "028_cuenta_cobro_fecha_transaccion"
branch_labels = None
depends_on = None


# Both the member NAMES (what SQLAlchemy emits) and the lowercase VALUES
# (migration 011's precedent). See the module docstring.
NEW_TIPO_VALUES = ("OTROS", "otros", "CDP", "cdp")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for value in NEW_TIPO_VALUES:
        op.execute(f"ALTER TYPE tipo_documento_fuente ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres does not support removing a value from an enum type.
    pass
