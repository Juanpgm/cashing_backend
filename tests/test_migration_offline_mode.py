"""Offline mode (`alembic upgrade --sql`) must keep working with the idempotent helpers.

`alembic/env.py` supports offline mode: migrations run against a MockConnection
that only collects SQL. There `op.get_bind()` is not inspectable
(`sa.inspect` raises `NoInspectionAvailable`), so a helper that always inspects
the live connection crashed every `upgrade --sql` that crossed migration 042.
Offline SQL generation cannot be idempotent (no database to look at), so the
helpers must fall through to the plain `op.*` call and emit the DDL.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.core.config import settings
from app.core.migration_helpers import (
    add_column_if_missing,
    column_exists,
    create_foreign_key_if_missing,
    create_index_if_missing,
    create_table_if_missing,
    create_unique_constraint_if_missing,
    index_exists,
    table_exists,
)

_ROOT = Path(__file__).resolve().parent.parent


@contextmanager
def _offline_ops(dialect: str = "postgresql") -> Iterator[io.StringIO]:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})
    with Operations.context(context):
        yield buffer


class TestRenderMigration042Offline:
    def test_upgrade_sql_emits_create_table_and_index(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # env.py reads settings.DATABASE_URL: aim it at a throwaway SQLite URL; offline
        # mode never connects, it only needs the dialect.
        monkeypatch.setattr(settings, "DATABASE_URL", f"sqlite+aiosqlite:///{(tmp_path / 'never.db').as_posix()}")
        output = io.StringIO()
        cfg = Config(output_buffer=output)
        cfg.set_main_option("script_location", str(_ROOT / "alembic"))

        command.upgrade(cfg, "041_cdp_enum_uppercase:042_paquete_job", sql=True)

        sql = output.getvalue()
        assert "CREATE TABLE paquete_job" in sql
        assert "CREATE INDEX ix_paquete_job_cuenta_cobro_id" in sql
        assert not (tmp_path / "never.db").exists(), "offline mode must not touch any database"


class TestHelpersOffline:
    def test_create_table_if_missing_emits_ddl_and_reports_created(self) -> None:
        with _offline_ops() as out:
            created = create_table_if_missing("widget", sa.Column("id", sa.Integer(), nullable=False))

        assert created is True
        assert "CREATE TABLE widget" in out.getvalue()

    def test_create_index_if_missing_emits_ddl(self) -> None:
        with _offline_ops() as out:
            created = create_index_if_missing("ix_widget_id", "widget", ["id"])

        assert created is True
        assert "CREATE INDEX ix_widget_id ON widget (id)" in out.getvalue()

    def test_add_column_if_missing_emits_ddl(self) -> None:
        with _offline_ops() as out:
            added = add_column_if_missing("widget", sa.Column("c", sa.Integer(), nullable=True))

        assert added is True
        assert "ALTER TABLE widget ADD COLUMN c INTEGER" in out.getvalue()

    def test_create_unique_constraint_if_missing_emits_ddl(self) -> None:
        with _offline_ops() as out:
            created = create_unique_constraint_if_missing("uq_widget_c", "widget", ["c"])

        assert created is True
        assert "ADD CONSTRAINT uq_widget_c UNIQUE (c)" in out.getvalue()

    def test_create_foreign_key_if_missing_emits_ddl(self) -> None:
        with _offline_ops() as out:
            created = create_foreign_key_if_missing("fk_widget_owner", "widget", "owner", ["owner_id"], ["id"])

        assert created is True
        assert "ADD CONSTRAINT fk_widget_owner FOREIGN KEY(owner_id) REFERENCES owner (id)" in out.getvalue()

    def test_existence_probes_answer_false_instead_of_raising(self) -> None:
        # Offline there is nothing to inspect; "unknown" is reported as "not there".
        with _offline_ops():
            assert table_exists("widget") is False
            assert column_exists("widget", "c") is False
            assert index_exists("widget", "ix") is False
