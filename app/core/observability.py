"""Small timing helper shared by agent-turn and LLM-call instrumentation.

Centralizes the ``time.perf_counter()`` start/elapsed pattern so call sites in
``agent_chat_service`` and ``litellm_adapter`` don't each hand-roll the same
few lines — see the ``radicacion-sin-friccion`` Phase 0 observability slice.
"""

from __future__ import annotations

import time


def elapsed_ms(start: float) -> float:
    """Milliseconds elapsed since ``start`` (a ``time.perf_counter()`` timestamp)."""
    return (time.perf_counter() - start) * 1000
