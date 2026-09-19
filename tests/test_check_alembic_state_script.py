"""Tests for `scripts/check_alembic_state.py`, the read-only post-deploy check.

The script is meant to run against PRODUCTION through
`railway run --service cashin-api -- uv run python scripts/check_alembic_state.py`,
so its two hard guarantees are tested explicitly: it only ever SELECTs, and it
never prints the database URL or its credentials — not even on a failure path.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from app.core.database import Base
from app.models.requisito_documento import RequisitoDocumento
from sqlalchemy.orm import Session

from tests.conftest import _IS_PG, TEST_DATABASE_URL

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_alembic_state.py"
_REV_043 = "043_checklist_primera_cuota_flags"


def _load() -> object:
    spec = importlib.util.spec_from_file_location("check_alembic_state", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sqlite_db(tmp_path: Path) -> tuple[str, sa.Engine]:
    import app.models  # noqa: F401

    file = tmp_path / "state.db"
    engine = sa.create_engine(f"sqlite:///{file.as_posix()}")
    Base.metadata.create_all(engine)
    return f"sqlite+aiosqlite:///{file.as_posix()}", engine


def _seed(engine: sa.Engine, *, version: str | None, rpc: bool) -> None:
    with Session(engine) as session:
        session.add(RequisitoDocumento(codigo="RPC", etiqueta="RPC", solo_primera_cuenta=rpc))
        session.add(RequisitoDocumento(codigo="CEDULA", etiqueta="Cedula", solo_primera_cuenta=True))
        session.commit()
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE alembic_version (version_num VARCHAR(255) NOT NULL PRIMARY KEY)"))
        if version is not None:
            conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": version})


def _report(url: str) -> dict[str, object]:
    module = _load()
    return asyncio.run(module.collect(url))  # type: ignore[attr-defined]


class TestCollect:
    def test_reports_version_at_head_and_flags(self, sqlite_db: tuple[str, sa.Engine]) -> None:
        url, engine = sqlite_db
        _seed(engine, version=_REV_043, rpc=True)

        report = _report(url)

        assert report["alembic_version"] == _REV_043
        assert report["head"] == _REV_043
        assert report["at_head"] is True
        assert report["paquete_job_table"] is True
        assert report["paquete_job_index"] is True
        assert report["flags"] == {"CEDULA": True, "RPC": True}

    def test_behind_head_is_reported_as_not_at_head(self, sqlite_db: tuple[str, sa.Engine]) -> None:
        url, engine = sqlite_db
        _seed(engine, version="041_cdp_enum_uppercase", rpc=False)

        report = _report(url)

        assert report["alembic_version"] == "041_cdp_enum_uppercase"
        assert report["at_head"] is False
        assert report["flags"]["RPC"] is False  # type: ignore[index]

    def test_empty_alembic_version_table_is_reported_not_crashed(self, sqlite_db: tuple[str, sa.Engine]) -> None:
        url, engine = sqlite_db
        _seed(engine, version=None, rpc=False)

        report = _report(url)

        assert report["alembic_version"] is None
        assert report["at_head"] is False

    def test_missing_alembic_version_table_is_reported_not_crashed(self, sqlite_db: tuple[str, sa.Engine]) -> None:
        url, _ = sqlite_db

        report = _report(url)

        assert report["alembic_version"] is None
        assert report["alembic_version_table"] is False

    def test_missing_requisitos_and_paquete_tables_do_not_crash(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.db"
        sa.create_engine(f"sqlite:///{empty.as_posix()}").dispose()

        report = _report(f"sqlite+aiosqlite:///{empty.as_posix()}")

        assert report["flags"] == {}
        assert report["paquete_job_table"] is False
        assert report["paquete_job_index"] is False

    def test_never_writes(self, sqlite_db: tuple[str, sa.Engine]) -> None:
        url, engine = sqlite_db
        _seed(engine, version=_REV_043, rpc=True)

        def snapshot() -> tuple[object, ...]:
            with engine.connect() as conn:
                return (
                    conn.execute(sa.text("SELECT version_num FROM alembic_version")).fetchall(),
                    conn.execute(sa.text("SELECT codigo, solo_primera_cuenta FROM requisitos_documento")).fetchall(),
                )

        before = snapshot()
        _report(url)
        assert snapshot() == before

    def test_source_contains_no_mutating_sql(self) -> None:
        source = _SCRIPT.read_text(encoding="utf-8")
        assert len(_sql_statements(source)) >= 3, "expected SELECTs plus SET TRANSACTION READ ONLY in the script"
        assert _mutating_sql(source) == []


_MUTATING = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE|COPY)\b", re.I)
_ALLOWED_START = re.compile(r"\s*(SELECT\b|SET\s+TRANSACTION\s+READ\s+ONLY\s*$)", re.I)
_SQL_START = re.compile(
    r"\s*(SELECT|SET|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|MERGE|COPY)\b", re.I
)


def _string_constants(source: str) -> list[str]:
    """Every string literal in `source` (any quoting, f-string parts included), minus docstrings."""
    tree = ast.parse(source)
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _sql_statements(source: str) -> list[str]:
    return [c for c in _string_constants(source) if _SQL_START.match(c)]


def _mutating_sql(source: str) -> list[str]:
    """Offending literals: SQL that is not SELECT / SET TRANSACTION READ ONLY, or any literal naming a write verb."""
    bad = [c for c in _sql_statements(source) if not _ALLOWED_START.match(c)]
    bad += [c for c in _string_constants(source) if _MUTATING.search(c) and c not in bad]
    return bad


class TestSourceScannerCatchesEveryLiteralStyle:
    @pytest.mark.parametrize(
        "literal",
        [
            '"DELETE FROM requisitos_documento"',
            "'UPDATE requisitos_documento SET x = 1'",
            '"""\n    INSERT INTO t VALUES (1)\n    """',
            "'''DROP TABLE t'''",
            'f"UPDATE {TABLE} SET x = 1"',
            "f'ALTER TABLE {TABLE} ADD COLUMN c INT'",
            '("SELECT 1; " "DELETE FROM t")',
            "sa.text('TRUNCATE t')",
        ],
    )
    def test_mutating_literal_is_reported(self, literal: str) -> None:
        source = f"TABLE = 't'\nstmt = {literal}\n"

        assert _mutating_sql(source) != []

    @pytest.mark.parametrize(
        "literal",
        ['"SELECT 1"', "'SELECT codigo FROM t'", '"SET TRANSACTION READ ONLY"', 'f"SELECT {COL} FROM t"'],
    )
    def test_read_only_literal_is_accepted(self, literal: str) -> None:
        assert _mutating_sql(f"COL = 'c'\nstmt = {literal}\n") == []

    def test_docstrings_are_not_treated_as_sql(self) -> None:
        source = '"""Every statement is a SELECT; we never UPDATE or DELETE."""\nx = 1\n'

        assert _mutating_sql(source) == []


class TestMain:
    def test_failure_never_prints_the_url_or_password(self, capsys: pytest.CaptureFixture[str]) -> None:
        module = _load()
        url = "postgresql+asyncpg://admin:S3kr3tPassw0rd@127.0.0.1:1/proddb"

        code = module.main(url)  # type: ignore[attr-defined]

        out = capsys.readouterr()
        text = out.out + out.err
        assert code == 1
        assert "S3kr3tPassw0rd" not in text
        assert "admin" not in text
        assert "proddb" not in text
        assert "postgresql+asyncpg://" not in text

    def test_driver_error_that_echoes_the_dsn_is_not_printed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Some drivers put the whole DSN in the exception message; only the
        # class name may reach the terminal.
        module = _load()
        url = "postgresql+asyncpg://admin:S3kr3tPassw0rd@db.example.com:5432/proddb"

        async def boom(_: str) -> dict[str, object]:
            raise RuntimeError(f"could not connect using {url}")

        monkeypatch.setattr(module, "collect", boom)

        code = module.main(url)  # type: ignore[attr-defined]

        out = capsys.readouterr()
        text = out.out + out.err
        assert code == 1
        assert "RuntimeError" in text
        assert "S3kr3tPassw0rd" not in text
        assert "proddb" not in text
        assert "db.example.com" not in text

    def test_success_output_never_contains_the_url(
        self, sqlite_db: tuple[str, sa.Engine], capsys: pytest.CaptureFixture[str]
    ) -> None:
        url, engine = sqlite_db
        _seed(engine, version=_REV_043, rpc=True)
        module = _load()

        code = module.main(url)  # type: ignore[attr-defined]

        text = capsys.readouterr().out
        assert code == 0
        assert _REV_043 in text
        assert "aiosqlite:///" not in text

    def test_behind_head_exits_nonzero(self, sqlite_db: tuple[str, sa.Engine]) -> None:
        url, engine = sqlite_db
        _seed(engine, version="041_cdp_enum_uppercase", rpc=False)
        module = _load()

        assert module.main(url) == 2  # type: ignore[attr-defined]


_PG_URL = "postgresql+asyncpg://prod_admin:S3kr3tPassw0rd@db-7f3a.proxy.example.com:5432/proddb"
_FAKE_REPORT: dict[str, object] = {
    "alembic_version": _REV_043,
    "alembic_version_table": True,
    "paquete_job_table": True,
    "paquete_job_index": True,
    "flags": {"CEDULA": True, "RPC": True},
}


class _FakeConn:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    async def execute(self, statement: Any) -> None:
        self.statements.append(str(statement))

    async def run_sync(self, _fn: Any) -> dict[str, object]:
        return dict(_FAKE_REPORT)

    async def rollback(self) -> None:
        return None


class _FakeConnect:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeEngine:
    """Stands in for the async engine: nothing here can reach a network."""

    def __init__(self, dialect: str) -> None:
        self.dialect = types.SimpleNamespace(name=dialect)
        self.statements: list[str] = []

    def connect(self) -> _FakeConnect:
        return _FakeConnect(_FakeConn(self.statements))

    async def dispose(self) -> None:
        return None


class TestMainWithAPostgresShapedUrl:
    def test_success_output_never_contains_host_user_password_or_database(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        module = _load()
        engine = _FakeEngine("postgresql")
        monkeypatch.setattr(module, "create_async_engine", lambda *_a, **_k: engine)

        code = module.main(_PG_URL)  # type: ignore[attr-defined]

        out = capsys.readouterr()
        text = out.out + out.err
        assert code == 0
        assert "postgresql" in text  # the dialect is fine to show
        for secret in ("db-7f3a", "proxy.example.com", "prod_admin", "S3kr3tPassw0rd", "proddb", "5432"):
            assert secret not in text, secret
        assert "host=" not in text

    def test_failure_output_never_contains_host_user_password_or_database(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        module = _load()

        async def boom(_: str) -> dict[str, object]:
            raise ConnectionRefusedError(f"cannot reach {_PG_URL}")

        monkeypatch.setattr(module, "collect", boom)

        code = module.main(_PG_URL)  # type: ignore[attr-defined]

        text = "".join(capsys.readouterr())
        assert code == 1
        assert text.strip() == "check failed: ConnectionRefusedError"

    def test_postgres_session_starts_read_only_as_its_first_statement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load()
        engine = _FakeEngine("postgresql")
        monkeypatch.setattr(module, "create_async_engine", lambda *_a, **_k: engine)

        asyncio.run(module.collect(_PG_URL))  # type: ignore[attr-defined]

        assert engine.statements == ["SET TRANSACTION READ ONLY"]

    def test_sqlite_session_does_not_issue_the_postgres_only_statement(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = _load()
        engine = _FakeEngine("sqlite")
        monkeypatch.setattr(module, "create_async_engine", lambda *_a, **_k: engine)

        asyncio.run(module.collect("sqlite+aiosqlite:///x.db"))  # type: ignore[attr-defined]

        assert engine.statements == []


class TestMainConfigFailure:
    def test_validation_error_on_config_import_never_leaks_input_values(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # pydantic's ValidationError str() embeds `input_value=...`; if it escaped as a
        # traceback the secret would land on the terminal.
        from pydantic_core import ValidationError

        def explode() -> ValidationError:
            return ValidationError.from_exception_data(
                "Settings",
                [
                    {
                        "type": "value_error",
                        "loc": ("DATABASE_URL",),
                        "input": "postgresql://u:F4keS3cret@h/db",
                        "ctx": {"error": ValueError("bad")},
                    }
                ],
            )

        class _BrokenConfig(types.ModuleType):
            def __getattr__(self, name: str) -> object:
                raise explode()

        monkeypatch.setitem(sys.modules, "app.core.config", _BrokenConfig("app.core.config"))
        module = _load()

        code = module.main()  # type: ignore[attr-defined]

        out = capsys.readouterr()
        assert code == 1
        assert "F4keS3cret" not in out.out + out.err
        assert out.out.strip() == "check failed: ValidationError"


@pytest.mark.skipif(not _IS_PG, reason="requires the PostgreSQL test mode (TEST_DATABASE_URL=postgresql...)")
class TestReadOnlyOnPostgres:
    """The server itself must refuse writes inside the check's session.

    Runs only against the disposable database the PG suite already targets
    (`scripts/test-postgres.sh`); never against a real deployment.
    """

    def test_write_inside_the_check_session_is_rejected(self) -> None:
        from sqlalchemy.exc import DBAPIError
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy.pool import NullPool

        module = _load()

        async def attempt() -> str:
            engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
            try:
                async with engine.connect() as conn:
                    await module._begin_read_only(conn)  # type: ignore[attr-defined]
                    state = (await conn.execute(sa.text("SHOW transaction_read_only"))).scalar_one()
                    with pytest.raises(DBAPIError):
                        await conn.execute(sa.text("CREATE TEMP TABLE _check_alembic_state_probe (i int)"))
                    await conn.rollback()
                    return str(state)
            finally:
                await engine.dispose()

        assert asyncio.run(attempt()) == "on"
