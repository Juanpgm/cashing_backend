"""Tests for Langfuse tracer (Phase 7)."""
from __future__ import annotations

import pytest


class TestNoopTracer:
    """When LANGFUSE_PUBLIC_KEY is not set, tracer is a no-op."""

    def test_tracer_context_manager_no_exception(self) -> None:
        from app.core.langfuse_client import _NoopTracer

        tracer = _NoopTracer()
        with tracer.trace("test_operation", input="hello") as span:
            span.update(output="world")
        # No exception → pass

    def test_noop_span_methods_are_silent(self) -> None:
        from app.core.langfuse_client import _NoopSpan

        span = _NoopSpan()
        span.update(output="result", metadata={"key": "value"})
        span.end()
        # No exception → pass

    def test_tracer_singleton_exported(self) -> None:
        from app.core import langfuse_client

        assert hasattr(langfuse_client, "tracer")
        tracer = langfuse_client.tracer
        # Must support context manager protocol
        assert hasattr(tracer, "trace")

    def test_noop_tracer_trace_returns_context_manager(self) -> None:
        from app.core.langfuse_client import _NoopTracer

        tracer = _NoopTracer()
        ctx = tracer.trace("op")
        # Should support __enter__ / __exit__
        assert hasattr(ctx, "__enter__")
        assert hasattr(ctx, "__exit__")

    def test_build_tracer_returns_noop_without_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without LANGFUSE_PUBLIC_KEY, factory returns _NoopTracer."""
        monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
        from app.core.langfuse_client import _build_tracer, _NoopTracer

        tracer = _build_tracer()
        assert isinstance(tracer, _NoopTracer)
