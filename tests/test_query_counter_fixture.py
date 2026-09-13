"""Tests for the reusable `QueryCounter` fixture itself (radicacion-sin-friccion,
slice 0.2) — NOT endpoint budgets (see `test_query_budgets.py` for those).

These prove the counting mechanism is trustworthy before any endpoint budget
is pinned against it: it must count real statements (including inside nested
SAVEPOINTs), reset cleanly between two measured calls in the same test, and
fail with a clear actual-vs-budget message.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.models.usuario import Usuario
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import QueryCounter

pytestmark = pytest.mark.asyncio


async def test_query_counter_counts_plain_statements(db: AsyncSession, query_counter: QueryCounter) -> None:
    query_counter.reset()

    await db.execute(text("SELECT 1"))
    await db.execute(text("SELECT 2"))

    assert query_counter.count == 2


async def test_query_counter_counts_queries_inside_nested_savepoint(
    db: AsyncSession, query_counter: QueryCounter
) -> None:
    """`session.begin_nested()` issues real `SAVEPOINT`/`RELEASE SAVEPOINT`
    statements over the cursor — the counter must not treat nested-transaction
    queries as invisible/free, or a budget could hide an N+1 introduced inside
    a nested transaction block (e.g. a per-item savepoint in a bulk import)."""
    query_counter.reset()

    async with db.begin_nested():
        db.add(
            Usuario(
                email="nested-savepoint@example.com",
                nombre="Nested",
                cedula="111222333",
                password_hash="x",
                rol="contratista",
                activo=True,
                creditos_disponibles=0,
            )
        )
        await db.flush()

    # At minimum: SAVEPOINT, the INSERT, RELEASE SAVEPOINT — 3 statements.
    # Asserting >= (not ==) keeps this robust to dialect-specific extra chatter
    # (e.g. an extra round-trip for autoincrement/RETURNING) while still
    # proving the nested-transaction statements were NOT silently dropped.
    assert query_counter.count >= 3, (
        f"expected at least SAVEPOINT + INSERT + RELEASE to be counted, got {query_counter.count}: "
        f"{query_counter.statements}"
    )
    assert any("SAVEPOINT" in s.upper() for s in query_counter.statements), (
        "no SAVEPOINT-related statement was captured — nested transaction queries are being hidden "
        f"from the counter: {query_counter.statements}"
    )


async def test_query_counter_resets_cleanly_between_two_calls(
    client: AsyncClient, test_user: dict[str, Any], query_counter: QueryCounter
) -> None:
    """No leakage: the second measured call must report only its own queries,
    not first_call + second_call."""
    query_counter.reset()
    r1 = await client.get("/api/v1/contratos/", headers=test_user["headers"])
    assert r1.status_code == 200, r1.text
    first_count = query_counter.count
    assert first_count > 0

    query_counter.reset()
    r2 = await client.get("/api/v1/contratos/", headers=test_user["headers"])
    assert r2.status_code == 200, r2.text
    second_count = query_counter.count

    # Same request, same data, issued twice back to back — if `reset()` leaked,
    # the second measurement would be roughly double the first instead of equal.
    assert second_count == first_count, (
        f"query_counter leaked between calls: first={first_count}, second={second_count} "
        "(expected equal — identical request against unchanged data)"
    )


async def test_query_counter_assert_budget_fails_with_actual_vs_budgeted_count(
    db: AsyncSession, query_counter: QueryCounter
) -> None:
    """The failure message must name both numbers so a regression is
    diagnosable from the assertion text alone, without a debugger."""
    query_counter.reset()
    for _ in range(5):
        await db.execute(text("SELECT 1"))

    with pytest.raises(AssertionError) as exc_info:
        query_counter.assert_budget(3, label="fake-endpoint")

    message = str(exc_info.value)
    assert "5 queries issued" in message
    assert "budget was 3" in message
    assert "fake-endpoint" in message


async def test_query_counter_assert_budget_passes_when_within_budget(
    db: AsyncSession, query_counter: QueryCounter
) -> None:
    query_counter.reset()
    await db.execute(text("SELECT 1"))

    query_counter.assert_budget(1, label="fake-endpoint")  # must not raise
