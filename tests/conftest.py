"""Pytest configuration and shared fixtures."""

import asyncio
import os
import sys
from collections.abc import AsyncGenerator, Generator
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
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Disable rate limiting in tests
limiter.enabled = False

# Ensure a valid Fernet key for adapters that encrypt OAuth tokens (Gmail/Drive/Calendar).
# The default placeholder in Settings is not a valid 32-byte base64 key.
if len(settings.TOKEN_ENCRYPTION_KEY) != 44:
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()

# Filesystem storage for the whole suite. A developer `.env` carrying
# STORAGE_PROVIDER=minio (or s3) leaked into the tests: every test that does not
# mock storage attempted a REAL S3 PUT and failed with "Could not connect to the
# endpoint URL: https://cashin-documentos.s3...". Tests that genuinely exercise S3
# build `S3StorageAdapter` explicitly against moto (see test_storage_adapters.py),
# and tests that need a specific root monkeypatch these back per test, so this
# session default only removes the network dependency.
_STORAGE_PROVIDER_ORIGINAL = settings.STORAGE_PROVIDER
_LOCAL_STORAGE_PATH_ORIGINAL = settings.LOCAL_STORAGE_PATH


@pytest.fixture(scope="session", autouse=True)
def storage_local_por_defecto(tmp_path_factory: pytest.TempPathFactory) -> Generator[None, None, None]:
    """Point storage at a throwaway directory instead of MinIO/S3."""
    settings.STORAGE_PROVIDER = "local"
    settings.LOCAL_STORAGE_PATH = str(tmp_path_factory.mktemp("storage"))
    try:
        yield
    finally:
        settings.STORAGE_PROVIDER = _STORAGE_PROVIDER_ORIGINAL
        settings.LOCAL_STORAGE_PATH = _LOCAL_STORAGE_PATH_ORIGINAL


_LLM_NETWORK_HINT = (
    "A test reached the real LLM network through {call}. Tests must mock their LLM seam "
    "(patch `get_llm` in the module under test, or `app.adapters.llm.get_llm`). "
    "Without this guard the call goes out to Gemini/Groq/Ollama with a 120s per-model "
    "timeout across a 3-model fallback chain, so an unmocked call stalls the suite for "
    "minutes instead of failing. Opt out only with @pytest.mark.live_llm / @pytest.mark.live."
)


@pytest.fixture(autouse=True)
def bloquear_red_llm(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast instead of hanging when a test forgets to mock its LLM seam.

    `LiteLLMAdapter` walks a 3-model fallback chain (Gemini -> Groq -> Ollama) with a
    120s timeout each, and most callers `except Exception` and fail OPEN. An unmocked
    call therefore never *fails* — it silently burns up to ~6 minutes of real network
    waiting and then returns the fallback value, which is exactly what made the full
    suite look like it was hanging. Raising here keeps the fail-open semantics (the
    caller still gets its fallback) while removing the network wait.

    Only the default (non-live) run is blocked: `tests/live/` opts back in via the
    `live_llm` / `live` markers, which `addopts` deselects by default anyway.
    """
    if request.node.get_closest_marker("live_llm") or request.node.get_closest_marker("live"):
        return

    import litellm

    def _bloqueado(nombre: str) -> Any:
        def _sync(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(_LLM_NETWORK_HINT.format(call=f"litellm.{nombre}"))

        return _sync

    def _bloqueado_async(nombre: str) -> Any:
        async def _async(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(_LLM_NETWORK_HINT.format(call=f"litellm.{nombre}"))

        return _async

    # `raising=True` is load-bearing: with `raising=False` a renamed litellm entrypoint
    # would make monkeypatch CREATE a dead attribute instead of failing, leaving a guard
    # that looks installed while real calls sail past it to the network. A rename must
    # break the suite loudly (AttributeError) so the guard gets updated.
    for nombre in ("completion", "embedding"):
        monkeypatch.setattr(litellm, nombre, _bloqueado(nombre), raising=True)
    for nombre in ("acompletion", "aembedding"):
        monkeypatch.setattr(litellm, nombre, _bloqueado_async(nombre), raising=True)


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
    _n = _test_db_name.lower()
    # Require "test" at a name boundary, not just any substring — so a real DB
    # coincidentally containing the letters (attestation, contest_backup, …) is
    # NOT accepted, while the usual test-DB conventions (cashin_test, test_db) are.
    if not (_n == "test" or _n.startswith("test_") or _n.endswith("_test") or "_test_" in _n):
        raise RuntimeError(
            f"Refusing destructive test setup (DROP SCHEMA CASCADE) against database "
            f"'{_test_db_name}': its name must be a disposable test DB (exact 'test', or "
            f"prefixed 'test_' / suffixed '_test', e.g. cashin_test). Point TEST_DATABASE_URL there."
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


class QueryCounter:
    """Counts SQL statements issued through the shared test engine.

    Reusable across every query-budget regression test (radicacion-sin-friccion,
    slice 0.2 onward — Phase 2 optimizations will assert their own *reductions*
    against this same counter). Attaches a `before_cursor_execute` listener to
    the sync engine underlying `engine_test` — the same engine backing both the
    `db` fixture and the ASGI `client` fixture (via `_override_get_db`), so a
    single counter instance sees every statement issued by an HTTP call made
    through `client`, direct `db` session use, or both combined.

    Nested SAVEPOINTs (`session.begin_nested()`) are NOT hidden from the count:
    SQLAlchemy emits `SAVEPOINT ...` / `RELEASE SAVEPOINT ...` (or `ROLLBACK TO
    SAVEPOINT ...`) as real `cursor.execute()` calls, so they go through this
    same listener like any other statement — see
    `test_query_counter_counts_queries_inside_nested_savepoint`.

    Postgres vs SQLite: the listener is attached to the *engine* object, so the
    counting mechanism itself is driver-agnostic (asyncpg vs aiosqlite both
    funnel through `before_cursor_execute`). What WILL differ between backends
    is the actual statement count for a given endpoint: Postgres round-trips
    for `DROP SCHEMA CASCADE`/enum handling differently than SQLite's
    create_all/drop_all (see `setup_database` above), and asyncpg may prepare
    statements the aiosqlite driver does not. Budgets pinned against SQLite are
    NOT guaranteed to hold byte-for-byte against `TEST_DATABASE_URL` pointed at
    Postgres — this is intentionally not exercised here (out of scope for this
    slice); a future slice that runs the budget suite against Postgres in CI
    should re-measure rather than assume parity.
    """

    def __init__(self, sync_engine: Engine) -> None:
        self._sync_engine = sync_engine
        self.count = 0
        self.statements: list[str] = []

    def _listener(
        self, conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool
    ) -> None:
        self.count += 1
        self.statements.append(statement)

    def reset(self) -> None:
        """Clear counts/statements. Call between two measured calls in the same
        test — each call's queries must be attributable to that call alone,
        never bleeding over from a previous request or from fixture setup."""
        self.count = 0
        self.statements.clear()

    def start(self) -> None:
        event.listen(self._sync_engine, "before_cursor_execute", self._listener)

    def stop(self) -> None:
        event.remove(self._sync_engine, "before_cursor_execute", self._listener)

    def assert_budget(self, budget: int, *, label: str = "") -> None:
        """Assert at most `budget` statements were issued since the last reset().

        Failure message names both the actual and budgeted count plus every
        captured statement, so a regression is diagnosable without re-running
        with a debugger.
        """
        if self.count > budget:
            detail = "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(self.statements))
            raise AssertionError(
                f"{label or 'query budget'}: {self.count} queries issued, budget was {budget} "
                f"(over by {self.count - budget}).\nStatements:\n{detail}"
            )


@pytest.fixture
def query_counter() -> Generator[QueryCounter, None, None]:
    """A `QueryCounter` already listening on the shared test engine.

    Starts counting immediately on fixture setup — call `.reset()` right
    before the call(s) you actually want to measure, since fixture/session
    setup before your test body runs will otherwise be included in the count.
    """
    counter = QueryCounter(engine_test.sync_engine)
    counter.start()
    try:
        yield counter
    finally:
        counter.stop()


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
