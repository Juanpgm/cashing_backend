"""Shared, event-loop-aware ``httpx.AsyncClient`` cache for outbound integrations.

Every outbound HTTP integration (SECOP, Microsoft Graph, Wompi, ...) used to
construct a brand-new ``httpx.AsyncClient`` on every single call
(``async with httpx.AsyncClient(...) as client: ...``). That throws away the
underlying connection pool after one request, forcing a fresh TCP/TLS
handshake on every outbound call. ``get_shared_client()`` fixes this by
reusing one ``httpx.AsyncClient`` per integration `name`, across calls.

Why NOT a plain module-level singleton (the pattern `app/core/langfuse_client.py`
uses, built once at import time)
---------------------------------------------------------------------------
This codebase has already been burned by exactly this class of bug once, for
a pooled *database* connection: `tests/conftest.py` documents that
`pytest-asyncio` (`asyncio_mode=auto`) runs each test in its OWN event loop,
and a previous module-level pooled DB engine handed later tests a connection
bound to a dead loop ("connection was closed in the middle of operation") —
fixed by switching to `NullPool` (fresh connection per operation, bound to
the current loop).

`httpx.AsyncClient` has the identical hazard: its connection pool opens its
first real socket lazily, under whichever event loop is running at that
moment. Reusing that same client instance from a DIFFERENT event loop later
breaks (anyio/asyncio errors resembling "attached to a different loop") —
exactly what happens across pytest-asyncio's function-scoped loops in this
suite's ~2800 tests, and, less obviously, could also happen in production if
anything ever ran Graph/SECOP/Wompi calls from more than one loop (background
workers, sync-context bridges, etc.).

The fix here is a small **lazy, event-loop-aware cache**: `get_shared_client`
reuses a client only within the loop that created it. A different loop
transparently gets its own fresh client — never one bound to a dead loop.

Why `WeakKeyDictionary` keyed by the loop OBJECT, not `id(loop)`
---------------------------------------------------------------------------
An earlier sketch of this design keyed a plain `dict` by `(name, id(loop))`.
That is unsound: CPython reuses `id()` values once an object is garbage
collected — once a finished event loop is collected, a *new, unrelated* loop
object can legally get the exact same `id()`. A plain `id()`-keyed dict would
then silently hand the new loop a client instance from the dead one (the
precise bug this module exists to prevent), and the dict entry for the dead
loop would also never be cleaned up (a slow leak across ~2800 per-test loops).

`WeakKeyDictionary` keyed by the loop object itself avoids both problems: it
tracks the actual live object via a weak reference for its whole lifetime, so
there is no id-reuse collision (a resurrected id can never match a *different*
live object's identity), and the entry is automatically dropped the moment
Python garbage-collects the loop — no manual cleanup / no `aclose()` hook
required (see "Explicitly NOT required this slice" in the design brief this
module implements: production has one long-lived loop for the process
lifetime, so the OS reclaims sockets at process exit either way).

Per-name caching (not one client for the whole process)
---------------------------------------------------------------------------
Different call sites want different client *configuration* (timeout,
`follow_redirects`, default `headers`, ...). `get_shared_client` caches one
client PER `name` PER loop, so unrelated integrations (or the same
integration with genuinely different kwargs, e.g. "secop-api" — the plain
Socrata JSON query — vs "secop-download" — file downloads that need
`follow_redirects=True` and a browser `User-Agent`) never collide on
configuration.

**Footgun to know about**: `client_kwargs` are only used the FIRST time a
given `(name, loop)` pair is constructed — they are cached with the client,
not re-applied on every call. Calling `get_shared_client("x", timeout=5)` and
later `get_shared_client("x", timeout=30)` within the same loop returns the
SAME client configured with `timeout=5`; the second call's kwargs are
silently ignored. This is why every call site in this codebase uses a
distinct `name` per distinct kwargs shape — never share a `name` across two
call sites that need different timeouts/headers. A call site that needs a
value to vary PER CALL (e.g. a caller-computed timeout budget) should pass
that value as a per-request override to the relevant `httpx` request method
(most `httpx` request methods accept a `timeout=` override) instead of
baking it into `client_kwargs`.
"""

from __future__ import annotations

import asyncio
from typing import Any
from weakref import WeakKeyDictionary

import httpx

# One dict of {integration name -> client} per event loop, held via a weak
# reference to the loop itself. See module docstring for why this must be
# `WeakKeyDictionary` keyed by the loop object and not a plain dict keyed by
# `id(loop)`.
_clients_by_loop: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]] = WeakKeyDictionary()


def get_shared_client(name: str, **client_kwargs: Any) -> httpx.AsyncClient:
    """Return a cached `httpx.AsyncClient` for `name`, reused within the
    CURRENT running event loop.

    A call from a different event loop transparently gets its own client —
    never one bound to a dead loop (see module docstring). A client that
    reports `is_closed` (e.g. closed explicitly elsewhere) is never reused; a
    fresh one is constructed in its place.

    The caller does NOT need (and must NOT do) `async with` / `aclose()` —
    the shared client outlives any single call. Closing it after one use
    would defeat the whole point and break the next caller in the same loop.

    `client_kwargs` are only applied the first time `name` is constructed for
    the current loop — see the "Footgun to know about" section of the module
    docstring.
    """
    loop = asyncio.get_running_loop()
    loop_clients = _clients_by_loop.get(loop)
    if loop_clients is None:
        loop_clients = {}
        _clients_by_loop[loop] = loop_clients

    client = loop_clients.get(name)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(**client_kwargs)
        loop_clients[name] = client
    return client


__all__ = ["get_shared_client"]
