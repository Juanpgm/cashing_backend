"""Tests for `app.core.startup_checks` (radicacion-sin-friccion 1.8).

Pure-function unit tests — no app boot, no DB, no lifespan. `app.main.lifespan`
itself just calls `assert_production_llm_fallback_configured` with the real
settings values; that wiring is intentionally NOT re-tested here (it would
require booting the whole app/DB/alembic lifecycle) — this file is the
authoritative test of the invariant itself.
"""

from __future__ import annotations

import pytest
from app.core.startup_checks import assert_production_llm_fallback_configured


def test_raises_when_production_and_fallback_model_is_empty() -> None:
    with pytest.raises(RuntimeError, match="LLM_PRODUCTION_FALLBACK_MODEL"):
        assert_production_llm_fallback_configured(is_production=True, fallback_model="")


def test_raises_when_production_and_fallback_model_is_whitespace_only() -> None:
    with pytest.raises(RuntimeError, match="LLM_PRODUCTION_FALLBACK_MODEL"):
        assert_production_llm_fallback_configured(is_production=True, fallback_model="   ")


def test_passes_silently_when_production_and_fallback_model_is_set() -> None:
    assert_production_llm_fallback_configured(is_production=True, fallback_model="ollama/llama3.1:8b")


def test_passes_silently_when_not_production_and_fallback_model_is_empty() -> None:
    """Local dev/test workflows that never set this variable must never break."""
    assert_production_llm_fallback_configured(is_production=False, fallback_model="")


def test_passes_silently_when_not_production_regardless_of_value() -> None:
    assert_production_llm_fallback_configured(is_production=False, fallback_model="anything")
