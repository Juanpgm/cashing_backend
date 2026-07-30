"""Regression test for the `025_imap_accounts_table` migration.

No backfill here (unlike 024) — just verifies upgrade()/downgrade() create and
drop a usable `imap_accounts` table with the expected unique constraint.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

_MIGRATION_PATH = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "025_imap_accounts_table.py"


def _load_migration() -> object:
    spec = importlib.util.spec_from_file_location("migration_025_imap_accounts_table", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration025ImapAccounts:
    def test_upgrade_creates_table_with_defaults_and_unique_constraint(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")
        user_id = uuid.uuid4()

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()  # type: ignore[attr-defined]

            conn.execute(
                sa.text(
                    "INSERT INTO imap_accounts "
                    "(id, usuario_id, email, host, username, password_encrypted, created_at, updated_at) "
                    "VALUES (:id, :uid, :email, :host, :username, :pwd, :created, :updated)"
                ),
                {
                    "id": uuid.uuid4().hex,
                    "uid": user_id.hex,
                    "email": "user@empresa.com",
                    "host": "mail.empresa.com",
                    "username": "user@empresa.com",
                    "pwd": "enc-password",
                    "created": "2026-01-01 00:00:00",
                    "updated": "2026-01-01 00:00:00",
                },
            )
            row = conn.execute(
                sa.text("SELECT port, use_ssl FROM imap_accounts WHERE usuario_id = :uid"), {"uid": user_id.hex}
            ).fetchone()

            # Unique (usuario_id, email) rejects a duplicate.
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO imap_accounts "
                        "(id, usuario_id, email, host, username, password_encrypted, created_at, updated_at) "
                        "VALUES (:id, :uid, :email, :host, :username, :pwd, :created, :updated)"
                    ),
                    {
                        "id": uuid.uuid4().hex,
                        "uid": user_id.hex,
                        "email": "user@empresa.com",
                        "host": "mail.empresa.com",
                        "username": "user@empresa.com",
                        "pwd": "enc-password-2",
                        "created": "2026-01-01 00:00:00",
                        "updated": "2026-01-01 00:00:00",
                    },
                )

        assert row.port == 993
        assert row.use_ssl in (1, True)

    def test_downgrade_drops_table(self) -> None:
        migration = _load_migration()
        engine = sa.create_engine("sqlite:///:memory:")

        with engine.begin() as conn:
            ctx = MigrationContext.configure(conn)
            with Operations.context(ctx):
                migration.upgrade()  # type: ignore[attr-defined]
                migration.downgrade()  # type: ignore[attr-defined]

            tables = conn.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type='table' AND name='imap_accounts'")
            ).fetchall()
        assert tables == []
