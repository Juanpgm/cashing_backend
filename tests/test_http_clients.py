"""Tests for `app.core.http_clients.get_shared_client` (perf/phase2-8-shared-httpx-client).

See `app/core/http_clients.py` module docstring for why this is a lazy,
event-loop-aware cache (WeakKeyDictionary keyed by the loop object) and NOT a
plain module-level singleton — mirrors the asyncpg/NullPool precedent in
`tests/conftest.py`.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
from app.core.http_clients import get_shared_client

pytestmark = pytest.mark.asyncio


async def test_same_loop_reuses_identical_client_instance() -> None:
    """Edge case 1: two sequential calls within the SAME event loop must
    return the IDENTICAL AsyncClient instance (identity, not just equal
    config) — the actual point of this slice (connection pool reuse)."""
    client_a = get_shared_client("test-reuse", timeout=5.0)
    client_b = get_shared_client("test-reuse", timeout=5.0)

    assert client_a is client_b


async def test_different_loop_does_not_reuse_and_does_not_raise() -> None:
    """Edge case 2: a call from a DIFFERENT event loop must NOT reuse the
    current loop's client, and must not raise or hang. Uses a separate OS
    thread running its own `asyncio.run(...)` loop (a brand-new, fully
    independent event loop — `asyncio.run` cannot be nested inside an
    already-running loop in the same thread)."""
    client_this_loop = get_shared_client("test-cross-loop", timeout=5.0)

    result: dict[str, httpx.AsyncClient] = {}
    error: dict[str, BaseException] = {}

    def _run_in_new_loop() -> None:
        async def _inner() -> httpx.AsyncClient:
            return get_shared_client("test-cross-loop", timeout=5.0)

        try:
            result["client"] = asyncio.run(_inner())
        except BaseException as exc:  # pragma: no cover - failure path asserted below
            error["error"] = exc

    thread = threading.Thread(target=_run_in_new_loop)
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive(), "cross-loop get_shared_client call hung"
    assert "error" not in error, f"cross-loop get_shared_client call raised: {error.get('error')}"
    assert result["client"] is not client_this_loop


# Module-level slot used by the two paired tests below to assert cross-test
# (i.e. cross-event-loop, since pytest-asyncio asyncio_mode=auto gives each
# test function its own loop) non-reuse without needing a background thread.
_captured_client: dict[str, httpx.AsyncClient] = {}


async def test_different_loop_via_separate_test_function_part_a() -> None:
    _captured_client["client"] = get_shared_client("test-cross-test-fn", timeout=5.0)


async def test_different_loop_via_separate_test_function_part_b() -> None:
    client_b = get_shared_client("test-cross-test-fn", timeout=5.0)
    assert client_b is not _captured_client["client"]


async def test_closed_client_is_not_reused() -> None:
    """Edge case 3: a client reporting `is_closed=True` (explicitly closed
    elsewhere, or a race) must NOT be reused — a fresh one is created."""
    client_a = get_shared_client("test-closed", timeout=5.0)
    await client_a.aclose()
    assert client_a.is_closed

    client_b = get_shared_client("test-closed", timeout=5.0)

    assert client_b is not client_a
    assert not client_b.is_closed


async def test_different_names_never_share_a_client() -> None:
    """Edge case 4: two different `name`s never share a client, even when
    called from the same loop."""
    client_a = get_shared_client("test-name-a", timeout=5.0)
    client_b = get_shared_client("test-name-b", timeout=5.0)

    assert client_a is not client_b


async def test_same_name_different_kwargs_keeps_first_configuration() -> None:
    """Edge case 5: calling the SAME `name` twice with DIFFERENT kwargs keeps
    the FIRST client's configuration — kwargs are only applied on first
    construction per (name, loop). This is intentional (documented in the
    module docstring's "Footgun to know about" section) and is exactly why
    call sites must use a distinct `name` per distinct kwargs shape."""
    client_a = get_shared_client("test-kwargs-footgun", timeout=5.0)
    client_b = get_shared_client("test-kwargs-footgun", timeout=99.0)

    assert client_a is client_b
    # The client keeps its ORIGINAL timeout (5.0s), the second call's
    # timeout=99.0 is silently ignored — documenting the actual behavior.
    assert client_b.timeout.read == 5.0
