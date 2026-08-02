"""Shared Postgres URL preparation: strip libpq-only params and pick SSL by host.

asyncpg (unlike libpq) does not understand ``sslmode`` / ``channel_binding`` in the
DSN, and SSL must be passed via ``connect_args``. Local/Docker Postgres runs without
SSL; managed remote databases (Neon, Railway) require it. Deciding by host — rather
than by ``ENVIRONMENT`` — lets local dev connect to Neon over SSL while still allowing
a plain local Docker Postgres.
"""

from __future__ import annotations

import ssl
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
_LIBPQ_ONLY_PARAMS = {"sslmode", "channel_binding"}


def prepare_pg_url(url: str) -> tuple[str, dict]:
    """Return ``(asyncpg-safe url, connect_args)`` for a database URL.

    - SQLite URLs are returned unchanged with empty connect args.
    - ``sslmode`` / ``channel_binding`` query params (which asyncpg can't parse) are
      stripped from the URL.
    - Remote hosts get SSL (encrypted, cert verification disabled for managed DBs);
      localhost/Docker gets SSL disabled.
    """
    if url.startswith("sqlite"):
        return url, {}

    parts = urlsplit(url)
    query = parse_qsl(parts.query)
    sslmode = next((v for k, v in query if k == "sslmode"), None)
    kept = [(k, v) for k, v in query if k not in _LIBPQ_ONLY_PARAMS]
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))

    # An explicit ``sslmode=disable`` wins over the host heuristic: a Docker Compose
    # service host like ``db`` is a plain local Postgres with SSL off, but it isn't in
    # _LOCAL_HOSTS, so without this it would be forced into a (rejected) SSL upgrade.
    # SAFETY: never honor sslmode=disable in PRODUCTION — a leftover/copy-pasted
    # disable in a real DATABASE_URL must not silently drop TLS to a managed DB.
    if sslmode == "disable":
        from app.core.config import settings

        if getattr(settings, "is_production", False):
            import structlog

            structlog.get_logger("core.db_ssl").warning(
                "ignoring_sslmode_disable_in_production",
                note="Refusing to disable TLS for a production database connection.",
            )
            # fall through to the SSL path below
        else:
            return clean, {"ssl": False}

    if (parts.hostname or "") in _LOCAL_HOSTS:
        return clean, {"ssl": False}

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return clean, {"ssl": ctx}
