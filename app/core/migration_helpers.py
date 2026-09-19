"""Idempotent building blocks for Alembic migrations.

Why this exists: `app.main.lifespan` runs `Base.metadata.create_all` FIRST
("create_all is the source of truth; migrations only carry ALTER deltas") and
only THEN `alembic upgrade head` (a failure there is merely a logged warning).
A migration that bare-`op.create_table(...)`s, `op.create_index(...)`es or
`op.add_column(...)`s an object the current models already describe therefore
collides with what create_all just built (`DuplicateTable` / `DuplicateColumn`),
aborts the whole upgrade, leaves `alembic_version` behind and silently skips
every later migration — the deploy still looks green. That is exactly what
`042_paquete_job` did to production.

Migrations numbered above 043 MUST use these helpers instead of the raw `op.*`
calls (enforced by `tests/test_migration_convention_guard.py`). Each helper
inspects the live connection first and is a no-op when the object is already
there. They work on PostgreSQL and SQLite, and only make sense inside a running
migration (they read `op.get_bind()`).

Name matching: PostgreSQL identifiers are compared exactly as stored (a quoted
mixed-case name is distinct from its lowercase twin); SQLite identifiers are
case-insensitive, so they are compared case-folded — creating `Foo` when `foo`
exists would collide there.

Note: only *existence* is checked, never shape. If an object exists with a
different definition (wrong column type, different index columns) the helper
does not repair it; that is a schema-drift problem for a dedicated migration.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op


def _fold(bind: sa.engine.Connection, name: str) -> str:
    return name.lower() if bind.dialect.name == "sqlite" else name


def table_exists(table_name: str, *, schema: str | None = None) -> bool:
    """True if `table_name` exists on the migration's connection."""
    bind = op.get_bind()
    wanted = _fold(bind, table_name)
    return any(_fold(bind, t) == wanted for t in sa.inspect(bind).get_table_names(schema=schema))


def column_exists(table_name: str, column_name: str, *, schema: str | None = None) -> bool:
    """True if `table_name` exists and has `column_name`. False (never raises) for a missing table."""
    if not table_exists(table_name, schema=schema):
        return False
    bind = op.get_bind()
    wanted = _fold(bind, column_name)
    return any(_fold(bind, c["name"]) == wanted for c in sa.inspect(bind).get_columns(table_name, schema=schema))


def index_exists(table_name: str, index_name: str, *, schema: str | None = None) -> bool:
    """True if `table_name` exists and carries an index called `index_name`.

    Scoped to the given table: an index of the same name on ANOTHER table does
    not count. False (never raises) for a missing table.
    """
    if not table_exists(table_name, schema=schema):
        return False
    bind = op.get_bind()
    wanted = _fold(bind, index_name)
    return any(_fold(bind, str(ix["name"])) == wanted for ix in sa.inspect(bind).get_indexes(table_name, schema=schema))


def create_table_if_missing(table_name: str, *columns: Any, **kwargs: Any) -> bool:
    """`op.create_table` unless the table already exists. Returns True when it created the table.

    Constraints and inline indexes passed alongside the columns are only created
    together with the table. If the table already exists (e.g. built by
    `create_all`) nothing is touched.
    """
    if table_exists(table_name, schema=kwargs.get("schema")):
        return False
    op.create_table(table_name, *columns, **kwargs)
    return True


def create_index_if_missing(
    index_name: str,
    table_name: str,
    columns: Sequence[Any],
    **kwargs: Any,
) -> bool:
    """`op.create_index` unless `table_name` already has an index `index_name`. Returns True when created."""
    if index_exists(table_name, index_name, schema=kwargs.get("schema")):
        return False
    op.create_index(index_name, table_name, list(columns), **kwargs)
    return True


def add_column_if_missing(table_name: str, column: sa.Column[Any], *, schema: str | None = None) -> bool:
    """`op.add_column` unless the column already exists. Returns True when it added the column.

    A missing TABLE is not swallowed: adding a column to a table that does not
    exist is a real bug in the migration chain and fails loudly.
    """
    if column_exists(table_name, column.name, schema=schema):
        return False
    op.add_column(table_name, column, schema=schema)
    return True
