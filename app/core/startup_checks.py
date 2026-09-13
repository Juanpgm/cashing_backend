"""Fail-fast startup invariants — checked once in `app.main.lifespan`.

Each check here is fail-LOUD by design: raising crashes the app on boot when a
production-only invariant is violated. This is deliberately stricter than the
`log.warning`-and-continue pattern `lifespan` already uses for e.g. a missing
`SECOP_APP_TOKEN` (SECOP degrades gracefully — partial document lists, still a
working app) or an unreachable DB at boot (some endpoints fail, others don't).
`LLM_PRODUCTION_FALLBACK_MODEL` is a different class of risk: without it,
`LiteLLMAdapter._get_model_chain` silently DROPS the third fallback slot in
production (see that method's own docstring) — a Gemini+Groq outage together
would then leave the agent with NO working model at all, and nothing short of
reading logs after the fact would ever surface it. Fail fast at boot instead.
"""

from __future__ import annotations


def assert_production_llm_fallback_configured(*, is_production: bool, fallback_model: str) -> None:
    """Raise `RuntimeError` when running in production without a real
    `LLM_PRODUCTION_FALLBACK_MODEL` configured. A no-op in every other
    environment regardless of the value — local/dev/test workflows that never
    set this variable must never break.
    """
    if is_production and not fallback_model.strip():
        raise RuntimeError(
            "LLM_PRODUCTION_FALLBACK_MODEL is empty in production — the LLM fallback chain "
            "would silently drop its last slot (see LiteLLMAdapter._get_model_chain), leaving "
            "no working model if the primary and secondary providers both fail. Set "
            "LLM_PRODUCTION_FALLBACK_MODEL before deploying."
        )
