"""Tests for `scripts/check_alembic_state.py`, the read-only post-deploy check.

The script is meant to run against PRODUCTION through
`railway run --service cashin-api -- uv run python scripts/check_alembic_state.py`,
so its two hard guarantees are tested explicitly: it only ever SELECTs, and it
never prints the database URL or its credentials — not even on a failure path.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from app.core.database import Base
from app.models.requisito_documento import RequisitoDocumento
from sqlalchemy.orm import Session

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
        statements = re.findall(r'text\(\s*"([^"]+)"', source)
        assert statements, "expected the script to issue SQL through text()"
        for stmt in statements:
            assert re.match(r"\s*(SELECT|SET TRANSACTION READ ONLY)\b", stmt, re.IGNORECASE), stmt
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE)\b", " ".join(statements), re.I)


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
