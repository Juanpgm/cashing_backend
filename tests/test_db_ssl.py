"""Exhaustive scenarios for prepare_pg_url — the security-critical SSL decision.

Pure/unit (no DB needed). Covers every branch: sqlite passthrough, local hosts,
Docker service host, remote managed DB, the production sslmode=disable guard, and
libpq-only param stripping that asyncpg can't parse.
"""

import ssl

import pytest

from app.core.config import settings
from app.core.db_ssl import prepare_pg_url


def test_sqlite_is_passed_through_untouched():
    url, args = prepare_pg_url("sqlite+aiosqlite:///:memory:")
    assert url == "sqlite+aiosqlite:///:memory:"
    assert args == {}


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_local_hosts_disable_ssl(host):
    # ::1 must be bracketed in a URL authority
    netloc_host = f"[{host}]" if host == "::1" else host
    _, args = prepare_pg_url(f"postgresql+asyncpg://u:p@{netloc_host}:5432/db")
    assert args == {"ssl": False}


def test_docker_service_host_with_sslmode_disable():
    # `db` (compose service) isn't a _LOCAL_HOST, so only the explicit sslmode=disable
    # keeps SSL off — and the param must be stripped (asyncpg can't parse it).
    url, args = prepare_pg_url("postgresql+asyncpg://u:p@db:5432/cashin?sslmode=disable")
    assert args == {"ssl": False}
    assert "sslmode" not in url


def test_remote_managed_db_gets_ssl_context():
    _, args = prepare_pg_url("postgresql+asyncpg://u:p@ep-1.neon.tech:5432/db")
    assert isinstance(args["ssl"], ssl.SSLContext)


def test_sslmode_disable_is_ignored_in_production(monkeypatch):
    # SECURITY: a leftover sslmode=disable against a REMOTE prod DB must NOT drop TLS.
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    _, args = prepare_pg_url("postgresql+asyncpg://u:p@ep-1.neon.tech:5432/db?sslmode=disable")
    assert isinstance(args["ssl"], ssl.SSLContext)


def test_sslmode_disable_honored_outside_production(monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    _, args = prepare_pg_url("postgresql+asyncpg://u:p@ep-1.neon.tech:5432/db?sslmode=disable")
    assert args == {"ssl": False}


def test_libpq_only_params_stripped_but_others_kept():
    url, _ = prepare_pg_url(
        "postgresql+asyncpg://u:p@ep-1.neon.tech/db"
        "?sslmode=require&channel_binding=require&application_name=cashin"
    )
    assert "sslmode" not in url
    assert "channel_binding" not in url
    assert "application_name=cashin" in url
