"""Async SQLAlchemy 2.0 database engine and session management."""

import ssl
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

_engine_kwargs: dict = {
    "echo": settings.is_development,
}
if not _is_sqlite:
    _engine_kwargs.update(
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
    )
    if settings.is_production:
        # Production (Railway): require SSL but skip cert verification for managed DBs
        _ssl_ctx = ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = ssl.CERT_NONE
        _engine_kwargs["connect_args"] = {"ssl": _ssl_ctx}
    else:
        # Local dev: disable SSL entirely (Docker / local PostgreSQL)
        _engine_kwargs["connect_args"] = {"ssl": False}

engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs)

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
