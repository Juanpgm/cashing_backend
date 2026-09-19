"""Unit tests for `create_unique_constraint_if_missing` / `create_foreign_key_if_missing`.

Same collision as tables/columns: `create_all` builds a table WITH its constraints
and never ALTERs an existing table, so a later migration that adds a named
unique/foreign-key constraint to a table create_all already produced hits a
duplicate-constraint error. The helpers look the constraint up by NAME and skip.

SQLite cannot ALTER constraints, so these two helpers are PostgreSQL-only: on a
live SQLite connection they must fail loudly (when the constraint is missing) or
skip (when it exists). The "creates it" path is asserted by recording the call to
the underlying `op.*` function with the SQLite guard neutralised (`calls` fixture);
the emitted DDL itself is covered by the offline-mode tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import op
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core import migration_helpers
from app.core.migration_helpers import create_foreign_key_if_missing, create_unique_constraint_if_missing


class _StringBuffer:
    def __init__(self) -> None:
        self.parts: list[str] = []

    def write(self, text: str) -> None:
        self.parts.append(text)

    def flush(self) -> None:
        return None


@contextmanager
def _ops(conn: sa.Connection) -> Iterator[None]:
    with Operations.context(MigrationContext.configure(conn)):
        yield


@pytest.fixture
def conn() -> Iterator[sa.Connection]:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE owner (id INTEGER PRIMARY KEY)"))
        connection.execute(
            sa.text(
                "CREATE TABLE widget ("
                "id INTEGER PRIMARY KEY, code VARCHAR(20), owner_id INTEGER, "
                "CONSTRAINT uq_widget_code UNIQUE (code), "
                "CONSTRAINT fk_widget_owner FOREIGN KEY (owner_id) REFERENCES owner (id))"
            )
        )
        yield connection
    engine.dispose()


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    recorded: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
    # The fixture connection is SQLite; neutralise the PostgreSQL-only guard so the
    # monkeypatched op.* recorder is reachable (the guard itself is tested below).
    monkeypatch.setattr(migration_helpers, "_require_alter_constraint_support", lambda helper: None)
    for name in ("create_unique_constraint", "create_foreign_key"):
        monkeypatch.setattr(op, name, lambda *a, _n=name, **k: recorded.append((_n, a, k)))
    return recorded


class TestCreateUniqueConstraintIfMissing:
    def test_existing_constraint_is_a_no_op(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            assert create_unique_constraint_if_missing("uq_widget_code", "widget", ["code"]) is False

        assert calls == []

    def test_missing_constraint_is_created_with_the_given_args(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            assert create_unique_constraint_if_missing("uq_widget_owner", "widget", ["owner_id"]) is True

        assert calls == [("create_unique_constraint", ("uq_widget_owner", "widget", ["owner_id"]), {})]

    def test_extra_keyword_arguments_are_forwarded(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            create_unique_constraint_if_missing("uq_widget_owner", "widget", ["owner_id"], deferrable=True)

        assert calls[0][2] == {"deferrable": True}

    def test_name_match_is_case_folded_on_sqlite(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            assert create_unique_constraint_if_missing("UQ_WIDGET_CODE", "widget", ["code"]) is False

        assert calls == []

    def test_lookup_is_by_name_not_by_columns(self, conn: sa.Connection, calls: list[Any]) -> None:
        # Documented limit: same columns under a DIFFERENT name is treated as missing.
        with _ops(conn):
            assert create_unique_constraint_if_missing("uq_other_name", "widget", ["code"]) is True

        assert len(calls) == 1

    def test_missing_table_is_not_swallowed(self, conn: sa.Connection, calls: list[Any]) -> None:
        # Fails loudly through the real op (here: the recorder is reached, no inspector crash).
        with _ops(conn):
            assert create_unique_constraint_if_missing("uq_x", "ghost", ["a"]) is True

        assert len(calls) == 1


class TestCreateForeignKeyIfMissing:
    def test_existing_constraint_is_a_no_op(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            assert create_foreign_key_if_missing("fk_widget_owner", "widget", "owner", ["owner_id"], ["id"]) is False

        assert calls == []

    def test_missing_constraint_is_created_with_the_given_args(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            created = create_foreign_key_if_missing(
                "fk_widget_owner_2", "widget", "owner", ["owner_id"], ["id"], ondelete="CASCADE"
            )

        assert created is True
        assert calls == [
            (
                "create_foreign_key",
                ("fk_widget_owner_2", "widget", "owner", ["owner_id"], ["id"]),
                {"ondelete": "CASCADE"},
            )
        ]

    def test_name_match_is_case_folded_on_sqlite(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            assert create_foreign_key_if_missing("FK_WIDGET_OWNER", "widget", "owner", ["owner_id"], ["id"]) is False

        assert calls == []

    def test_unnamed_foreign_keys_on_the_table_do_not_match(self, calls: list[Any]) -> None:
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE owner (id INTEGER PRIMARY KEY)"))
            connection.execute(
                sa.text("CREATE TABLE gadget (id INTEGER PRIMARY KEY, owner_id INTEGER REFERENCES owner (id))")
            )
            with _ops(connection):
                assert create_foreign_key_if_missing("fk_gadget_owner", "gadget", "owner", ["owner_id"], ["id"]) is True
        engine.dispose()

        assert len(calls) == 1

    def test_missing_table_is_not_swallowed(self, conn: sa.Connection, calls: list[Any]) -> None:
        with _ops(conn):
            assert create_foreign_key_if_missing("fk_x", "ghost", "owner", ["a"], ["id"]) is True

        assert len(calls) == 1


class TestPostgresOnlyOnRealSqlite:
    """No monkeypatched `op`: a real SQLite connection and the real alembic operations."""

    def test_unique_constraint_fails_loudly_and_names_the_alternative(self, conn: sa.Connection) -> None:
        with _ops(conn), pytest.raises(NotImplementedError) as excinfo:
            create_unique_constraint_if_missing("uq_widget_owner", "widget", ["owner_id"])

        message = str(excinfo.value)
        assert "create_unique_constraint_if_missing" in message
        assert "PostgreSQL-only" in message
        assert "batch_alter_table" in message
        assert "migration-guard: ignore" in message

    def test_foreign_key_fails_loudly_and_names_the_alternative(self, conn: sa.Connection) -> None:
        with _ops(conn), pytest.raises(NotImplementedError) as excinfo:
            create_foreign_key_if_missing("fk_widget_owner_2", "widget", "owner", ["owner_id"], ["id"])

        message = str(excinfo.value)
        assert "create_foreign_key_if_missing" in message
        assert "PostgreSQL-only" in message
        assert "batch_alter_table" in message

    def test_unique_constraint_that_already_exists_still_skips(self, conn: sa.Connection) -> None:
        with _ops(conn):
            assert create_unique_constraint_if_missing("uq_widget_code", "widget", ["code"]) is False

    def test_foreign_key_that_already_exists_still_skips(self, conn: sa.Connection) -> None:
        with _ops(conn):
            assert create_foreign_key_if_missing("fk_widget_owner", "widget", "owner", ["owner_id"], ["id"]) is False

    def test_missing_table_on_sqlite_also_fails_loudly(self, conn: sa.Connection) -> None:
        with _ops(conn), pytest.raises(NotImplementedError, match="PostgreSQL-only"):
            create_unique_constraint_if_missing("uq_x", "ghost", ["a"])

    def test_offline_mode_still_falls_through_to_the_plain_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: list[str] = []
        monkeypatch.setattr(op, "create_unique_constraint", lambda *a, **k: recorded.append("uq"))
        monkeypatch.setattr(op, "create_foreign_key", lambda *a, **k: recorded.append("fk"))
        buffer = _StringBuffer()
        context = MigrationContext.configure(dialect_name="sqlite", opts={"as_sql": True, "output_buffer": buffer})
        with Operations.context(context):
            assert create_unique_constraint_if_missing("uq_a", "widget", ["a"]) is True
            assert create_foreign_key_if_missing("fk_a", "widget", "owner", ["a"], ["id"]) is True

        assert recorded == ["uq", "fk"]
