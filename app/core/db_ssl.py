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
    kept = [(k, v) for k, v in parse_qsl(parts.query) if k not in _LIBPQ_ONLY_PARAMS]
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment))

    if (parts.hostname or "") in _LOCAL_HOSTS:
        return clean, {"ssl": False}

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return clean, {"ssl": ctx}
