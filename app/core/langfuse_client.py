"""Langfuse LLM observability integration (Phase 7).

Provides a singleton Langfuse client gated behind the ``LANGFUSE_PUBLIC_KEY``
environment variable.  When no key is configured (e.g., local dev / CI), all
calls are no-ops so the rest of the codebase can use the tracer unconditionally.

Usage::

    from app.core.langfuse_client import tracer

    with tracer.trace("agent_run", user_id=str(user.id), metadata={"modo": "CUENTA_COBRO"}) as span:
        ...
        span.end(output={"quality": 0.92})
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator
from typing import Any

import structlog

from app.core.config import settings

logger = structlog.get_logger("core.langfuse_client")


# ---------------------------------------------------------------------------
# No-op fallback (when Langfuse is not configured / not installable)
# ---------------------------------------------------------------------------


class _NoopSpan:
    """Silent no-op span that accepts all calls without error."""

    def end(self, **kwargs: Any) -> None:  # noqa: ANN401
        pass

    def update(self, **kwargs: Any) -> None:
        pass

    def score(self, **kwargs: Any) -> None:
        pass


class _NoopTracer:
    """Silent no-op tracer — used when LANGFUSE_PUBLIC_KEY is not configured."""

    @contextlib.contextmanager
    def trace(self, name: str, **kwargs: Any) -> Generator[_NoopSpan, None, None]:
        yield _NoopSpan()

    def generation(self, **kwargs: Any) -> _NoopSpan:
        return _NoopSpan()

    def flush(self) -> None:
        pass

    @property
    def enabled(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Real tracer (wraps Langfuse client)
# ---------------------------------------------------------------------------


class _LangfuseTracer:
    """Thin wrapper around the Langfuse Python client."""

    def __init__(self) -> None:
        from langfuse import Langfuse  # type: ignore[import-untyped]

        self._client = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_HOST,
        )

    @contextlib.contextmanager
    def trace(self, name: str, **kwargs: Any) -> Generator[Any, None, None]:
        """Create a Langfuse trace and yield it as a span context."""
        trace = self._client.trace(name=name, **kwargs)
        try:
            yield trace
        finally:
            self._client.flush()

    def generation(self, **kwargs: Any) -> Any:
        """Record a single LLM generation event."""
        return self._client.generation(**kwargs)

    def flush(self) -> None:
        self._client.flush()

    @property
    def enabled(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Factory — build the appropriate tracer once at module import
# ---------------------------------------------------------------------------


def _build_tracer() -> _NoopTracer | _LangfuseTracer:
    if not settings.LANGFUSE_PUBLIC_KEY:
        # Structlog, not a silent return: the radicacion-sin-friccion 0.3 adversarial
        # review flagged that call sites wiring `tracer.generation(...)` around the
        # LLM adapter can read as "Langfuse tracing is live" when it silently is not
        # — this line makes the no-op state visible in Railway's JSON log stream at
        # startup instead of only in a source-code comment.
        logger.info(
            "langfuse_tracing_disabled",
            reason="LANGFUSE_PUBLIC_KEY not set — every tracer.*() call below is a no-op",
        )
        return _NoopTracer()
    try:
        return _LangfuseTracer()
    except Exception as exc:  # pragma: no cover
        # error, not warning: a key WAS configured (someone intended tracing to work),
        # so this is a real misconfiguration — e.g. the `langfuse` package is not a
        # declared dependency yet (confirmed absent from pyproject.toml as of this
        # comment) and must be added via `make lock` before this path can succeed.
        logger.error("langfuse_init_failed", error=str(exc))
        return _NoopTracer()


tracer: _NoopTracer | _LangfuseTracer = _build_tracer()

__all__ = ["tracer"]
