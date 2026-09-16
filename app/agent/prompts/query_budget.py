"""Round-robin query-budget allocation shared by the Gmail/Drive/Calendar builders.

Round-2 fix for the confirmed CRITICAL finding "contract-number variants consume
the entire query budget in all three providers". The previous design built one
flat FIFO list (contract-level terms first, then obligación #1's terms, then
obligación #2's, ...) and truncated it to the budget, so for a contract whose
number expands into many variants the truncation point fell inside obligación
#1 and every later obligación got ZERO queries.

The fix is allocation, not a bigger budget: contract-level terms get their own
small reserved block, and whatever remains is dealt ROUND-ROBIN across the
obligaciones — one term each, then a second term each, and so on. That
guarantees a per-obligación floor no matter how many variants or how many
obligaciones a contract has.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def round_robin(groups: Sequence[Sequence[T]], budget: int) -> list[T]:
    """Deal up to `budget` items from `groups`, one per group per pass.

    Every group contributes its first item before any group contributes a
    second, so a per-group floor of `budget // len(groups)` is guaranteed.
    Exhausted groups are skipped rather than padded, so a short group never
    wastes a slot. Duplicates across groups are dropped, keeping the earliest
    position (the providers dedupe queries downstream anyway; doing it here
    means a duplicate does not silently consume another group's slot).
    """
    if budget <= 0 or not groups:
        return []

    out: list[T] = []
    seen: set[T] = set()
    cursors = [0] * len(groups)

    while len(out) < budget:
        progressed = False
        for gi, group in enumerate(groups):
            if len(out) >= budget:
                break
            # Advance past items already emitted by an earlier group.
            while cursors[gi] < len(group) and group[cursors[gi]] in seen:
                cursors[gi] += 1
            if cursors[gi] >= len(group):
                continue
            item = group[cursors[gi]]
            cursors[gi] += 1
            seen.add(item)
            out.append(item)
            progressed = True
        if not progressed:
            break

    return out
