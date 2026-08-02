"""Pytest configuration and shared fixtures."""

import asyncio
import os
import sys
from collections.abc import AsyncGenerator
from typing import Any

import app.models  # noqa: F401 — register all models for Base.metadata
import pytest
from app.core.config import settings
from app.core.database import Base, get_db
from app.core.rate_limit import limiter
from app.core.security import create_access_token, hash_password
from app.main import app as fastapi_app
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Disable rate limiting in tests
limiter.enabled = False

# Ensure a valid Fernet key for adapters that encrypt OAuth tokens (Gmail/Drive/Calendar).
# The default placeholder in Settings is not a valid 32-byte base64 key.
if len(settings.TOKEN_ENCRYPTION_KEY) != 44:
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()

# In-memory SQLite for tests by default; override with TEST_DATABASE_URL to run
# the suite against Postgres (e.g. postgresql+asyncpg://cashin:cashin_local@localhost:5432/cashin).
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
_IS_PG = TEST_DATABASE_URL.startswith("postgresql")

# SAFETY: the autouse setup fixture runs `DROP SCHEMA public CASCADE` before every
# test. Refuse to do that unless the target database name marks it as disposable —
# a stray TEST_DATABASE_URL pointing at a real dev/staging DB must never be wiped.
# Fail fast at import, before any test runs.
if _IS_PG:
    _test_db_name = TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in _test_db_name.lower():
        raise RuntimeError(
            f"Refusing destructive test setup (DROP SCHEMA CASCADE) against database "
            f"'{_test_db_name}': its name must contain 'test'. Point TEST_DATABASE_URL "
            f"at a disposable test database (e.g. cashin_test)."
        )

# asyncpg breaks on the Windows ProactorEventLoop (Python 3.12 default): rapid
# connect/close over TCP loopback raises "WinError 64 / connection was closed in
# the middle of operation". SelectorEventLoop is asyncpg's supported loop on Windows.
# Only switch when targeting Postgres; SQLite (aiosqlite, thread-based) is unaffected,
# and production runs on Linux so this is a Windows-dev-only concern.
if sys.platform == "win32" and _IS_PG:
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# pytest-asyncio (asyncio_mode=auto) runs each test in its own event loop. asyncpg
# connections are loop-bound, so a module-level pooled engine hands the next test a
# connection from a dead loop ("connection was closed in the middle of operation").
# NullPool forces a fresh connection per operation, bound to the current loop.
# SQLite in-memory keeps its default pool (a NullPool would drop the :memory: schema).
_engine_kwargs: dict[str, Any] = {"echo": False}
if _IS_PG:
    from sqlalchemy.pool import NullPool

    _engine_kwargs["poolclass"] = NullPool

engine_test = create_async_engine(TEST_DATABASE_URL, **_engine_kwargs)
async_session_test = async_sessionmaker(engine_test, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def setup_database() -> AsyncGenerator[None, None]:
    """Give each test a fresh schema.

    On Postgres we drop the whole ``public`` schema instead of ``MetaData.drop_all``:
    a CASCADE wipes tables, named ENUM types (estado_cuenta_cobro, etc.) and the
    use_alter FK constraint in one statement. ``drop_all`` leaves ENUM types behind
    (next test's create_all then fails with "type already exists") and can't order the
    contratos<->documentos_fuente cycle. Dropping at *setup* also self-heals a DB left
    dirty by a previously crashed run. SQLite keeps the original cheap create_all/drop_all.
    """
    async with engine_test.begin() as conn:
        if _IS_PG:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
            # DROP SCHEMA CASCADE also drops the pgvector extension; recreate it so the
            # semantic-search queries (embedding::vector) work. Needs the pgvector image.
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    yield
    if not _IS_PG:
        async with engine_test.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)


async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_test() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


fastapi_app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture
async def db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_test() as session:
        yield session


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def test_user(db: AsyncSession) -> dict[str, Any]:
    """Create a test user and return user dict with access_token."""
    from app.models.usuario import Usuario

    user = Usuario(
        email="test@example.com",
        nombre="Test User",
        cedula="123456789",
        telefono="+573001234567",
        password_hash=hash_password("TestPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    token = create_access_token(subject=str(user.id), role=user.rol)
    return {
        "user": user,
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }
