"""Async SQLAlchemy 2.0 database engine and session management."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings
from app.core.db_ssl import prepare_pg_url

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
# SSL is decided by host (localhost → off, remote managed DB → on), so local dev
# can connect to Neon/Railway over SSL without flipping ENVIRONMENT.
_db_url, _connect_args = prepare_pg_url(settings.DATABASE_URL)

_engine_kwargs: dict = {
    "echo": settings.is_development,
}
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args=_connect_args,
    )

engine = create_async_engine(_db_url, **_engine_kwargs)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
