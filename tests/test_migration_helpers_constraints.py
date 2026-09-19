"""Unit tests for `create_unique_constraint_if_missing` / `create_foreign_key_if_missing`.

Same collision as tables/columns: `create_all` builds a table WITH its constraints
and never ALTERs an existing table, so a later migration that adds a named
unique/foreign-key constraint to a table create_all already produced hits a
duplicate-constraint error. The helpers look the constraint up by NAME and skip.

SQLite cannot ALTER constraints, so the "creates it" path is asserted by
recording the call to the underlying `op.*` function; the emitted DDL itself is
covered by the offline-mode tests.
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
from app.core.migration_helpers import create_foreign_key_if_missing, create_unique_constraint_if_missing


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
