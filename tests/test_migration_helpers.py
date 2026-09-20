"""Unit tests for `app.core.migration_helpers`.

The helpers make a migration idempotent against the boot order in
`app.main.lifespan` (`Base.metadata.create_all` runs BEFORE `alembic upgrade
head`, so any "create X" migration collides with what create_all already
built). They are exercised here through Alembic's `Operations` /
`MigrationContext` API against a throwaway in-memory SQLite DB, the same pattern
as `test_migration_043_checklist_primera_cuota_flags.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.migration_helpers import (
    add_column_if_missing,
    column_exists,
    create_index_if_missing,
    create_table_if_missing,
    index_exists,
    table_exists,
)


@contextmanager
def _ops(conn: sa.Connection) -> Iterator[None]:
    with Operations.context(MigrationContext.configure(conn)):
        yield


@pytest.fixture
def conn() -> Iterator[sa.Connection]:
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        yield connection
    engine.dispose()


def _make_widget(conn: sa.Connection) -> None:
    conn.execute(sa.text("CREATE TABLE widget (id INTEGER PRIMARY KEY, name VARCHAR(20))"))


class TestTableExists:
    def test_true_for_existing_table(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert table_exists("widget") is True

    def test_false_for_missing_table(self, conn: sa.Connection) -> None:
        with _ops(conn):
            assert table_exists("widget") is False

    def test_lookup_is_by_exact_table_not_prefix(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert table_exists("widg") is False
            assert table_exists("widgets") is False

    def test_sqlite_lookup_is_case_insensitive(self, conn: sa.Connection) -> None:
        # SQLite identifiers are case-insensitive: creating "Widget" when
        # "widget" exists would collide, so the guard must see it as existing.
        _make_widget(conn)
        with _ops(conn):
            assert table_exists("WIDGET") is True

    def test_quoted_name_with_spaces_and_quotes(self, conn: sa.Connection) -> None:
        weird = 'we"ird table'
        conn.execute(sa.text('CREATE TABLE "we""ird table" (id INTEGER PRIMARY KEY)'))
        with _ops(conn):
            assert table_exists(weird) is True
            assert table_exists("weird table") is False


class TestColumnExists:
    def test_true_for_existing_column(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert column_exists("widget", "name") is True

    def test_false_for_missing_column(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert column_exists("widget", "color") is False

    def test_false_when_table_is_missing(self, conn: sa.Connection) -> None:
        # Must not raise NoSuchTableError.
        with _ops(conn):
            assert column_exists("nope", "name") is False

    def test_sqlite_lookup_is_case_insensitive(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert column_exists("widget", "NAME") is True

    def test_same_column_name_on_another_table_does_not_count(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text("CREATE TABLE gadget (id INTEGER PRIMARY KEY, color VARCHAR(10))"))
        with _ops(conn):
            assert column_exists("widget", "color") is False
            assert column_exists("gadget", "color") is True


class TestIndexExists:
    def test_true_for_existing_index(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text("CREATE INDEX ix_widget_name ON widget (name)"))
        with _ops(conn):
            assert index_exists("widget", "ix_widget_name") is True

    def test_false_for_missing_index(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert index_exists("widget", "ix_widget_name") is False

    def test_false_when_table_is_missing(self, conn: sa.Connection) -> None:
        with _ops(conn):
            assert index_exists("nope", "ix_nope_name") is False

    def test_table_exists_but_index_missing(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text("CREATE INDEX ix_widget_other ON widget (id, name)"))
        with _ops(conn):
            assert table_exists("widget") is True
            assert index_exists("widget", "ix_widget_name") is False

    def test_index_on_a_different_table_does_not_count(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text("CREATE TABLE gadget (id INTEGER PRIMARY KEY, name VARCHAR(20))"))
        conn.execute(sa.text("CREATE INDEX ix_gadget_name ON gadget (name)"))
        with _ops(conn):
            assert index_exists("gadget", "ix_gadget_name") is True
            assert index_exists("widget", "ix_gadget_name") is False

    def test_sqlite_lookup_is_case_insensitive(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text("CREATE INDEX ix_widget_name ON widget (name)"))
        with _ops(conn):
            assert index_exists("widget", "IX_WIDGET_NAME") is True

    def test_quoted_index_name(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text('CREATE INDEX "ix widget ""q""" ON widget (name)'))
        with _ops(conn):
            assert index_exists("widget", 'ix widget "q"') is True


class TestCreateTableIfMissing:
    @staticmethod
    def _create() -> bool:
        return create_table_if_missing(
            "widget",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(20), nullable=True),
        )

    def test_creates_a_missing_table_and_reports_it(self, conn: sa.Connection) -> None:
        with _ops(conn):
            assert self._create() is True
            assert table_exists("widget") is True

    def test_is_a_noop_when_the_table_exists_and_keeps_its_data(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text("INSERT INTO widget (id, name) VALUES (1, 'kept')"))
        with _ops(conn):
            assert self._create() is False
        assert conn.execute(sa.text("SELECT name FROM widget WHERE id = 1")).scalar_one() == "kept"

    def test_called_twice_is_idempotent(self, conn: sa.Connection) -> None:
        with _ops(conn):
            assert self._create() is True
            assert self._create() is False


class TestCreateIndexIfMissing:
    @staticmethod
    def _create(name: str = "ix_widget_name", table: str = "widget") -> bool:
        return create_index_if_missing(name, table, ["name"])

    def test_creates_a_missing_index(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert self._create() is True
            assert index_exists("widget", "ix_widget_name") is True

    def test_is_a_noop_when_the_index_exists(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        conn.execute(sa.text("CREATE INDEX ix_widget_name ON widget (name)"))
        with _ops(conn):
            assert self._create() is False

    def test_called_twice_is_idempotent(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert self._create() is True
            assert self._create() is False

    def test_supports_unique_indexes(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert create_index_if_missing("uq_widget_name", "widget", ["name"], unique=True) is True
        conn.execute(sa.text("INSERT INTO widget (id, name) VALUES (1, 'a')"))
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(sa.text("INSERT INTO widget (id, name) VALUES (2, 'a')"))

    def test_same_index_name_free_on_another_table_still_fails_loudly_on_sqlite(self, conn: sa.Connection) -> None:
        # An index name is schema-global in SQLite/PG. Being taken by a
        # DIFFERENT table is a genuine conflict, not something to swallow:
        # index_exists(widget, ...) is False, so the create is attempted and the
        # database raises. The helper must not hide a real naming collision.
        _make_widget(conn)
        conn.execute(sa.text("CREATE TABLE gadget (id INTEGER PRIMARY KEY, name VARCHAR(20))"))
        conn.execute(sa.text("CREATE INDEX ix_shared ON gadget (name)"))
        with _ops(conn), pytest.raises(sa.exc.OperationalError):
            self._create(name="ix_shared", table="widget")


class TestAddColumnIfMissing:
    def test_adds_a_missing_column(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert add_column_if_missing("widget", sa.Column("color", sa.String(10), nullable=True)) is True
            assert column_exists("widget", "color") is True

    def test_is_a_noop_when_the_column_exists(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert add_column_if_missing("widget", sa.Column("name", sa.String(20), nullable=True)) is False

    def test_called_twice_is_idempotent(self, conn: sa.Connection) -> None:
        _make_widget(conn)
        with _ops(conn):
            assert add_column_if_missing("widget", sa.Column("color", sa.String(10), nullable=True)) is True
            assert add_column_if_missing("widget", sa.Column("color", sa.String(10), nullable=True)) is False

    def test_missing_table_still_fails_loudly(self, conn: sa.Connection) -> None:
        # Adding a column to a table that does not exist is a real bug in the
        # migration chain — never silently skipped.
        with _ops(conn), pytest.raises(sa.exc.OperationalError):
            add_column_if_missing("nope", sa.Column("color", sa.String(10), nullable=True))
