"""Regression guard for the suite-wide LLM network block (`tests/conftest.py`).

Why this file exists: without the `bloquear_red_llm` autouse fixture, any test that
forgets to mock its LLM seam silently walks `LiteLLMAdapter`'s 3-model fallback chain
(Gemini -> Groq -> Ollama, 120s timeout each) against the real network. Because almost
every caller `except Exception` and fails OPEN, such a test still *passes* — it just
stalls for ~30s-6min per call. Two of those stalls used to make the full suite look
like it hung forever (`tests/journey/test_full_radicacion_journey.py` and
`tests/test_agente_cadena_completa.py`).

These tests fail loudly if the guard is ever weakened or deleted, so the hang cannot
come back unnoticed.
"""

from __future__ import annotations

import pytest


async def test_acompletion_esta_bloqueada() -> None:
    """The async completion entrypoint must raise instead of reaching the network."""
    import litellm

    with pytest.raises(RuntimeError, match="reached the real LLM network"):
        await litellm.acompletion(model="groq/llama3-8b-8192", messages=[{"role": "user", "content": "hola"}])


async def test_aembedding_esta_bloqueada() -> None:
    """The async embedding entrypoint must raise instead of reaching the network."""
    import litellm

    with pytest.raises(RuntimeError, match="reached the real LLM network"):
        await litellm.aembedding(model="gemini/text-embedding-004", input=["hola"])


def test_entrypoints_sincronos_estan_bloqueados() -> None:
    """The sync entrypoints matter too: `litellm.embedding` is what the Gemini batch
    embedding path lands on, and it blocks the event loop thread when it goes out."""
    import litellm

    with pytest.raises(RuntimeError, match="reached the real LLM network"):
        litellm.completion(model="groq/llama3-8b-8192", messages=[{"role": "user", "content": "hola"}])
    with pytest.raises(RuntimeError, match="reached the real LLM network"):
        litellm.embedding(model="gemini/text-embedding-004", input=["hola"])


#: Wall-clock ceiling for the guarded adapter call below, set from measurement.
#:
#: The bound has to be a real *timing* assertion because the error message alone does
#: NOT discriminate: with no API keys configured, the UNGUARDED adapter also ends in
#: `All LLM models failed` — it just takes far longer getting there.
#:
#: Measured on a dev machine (Windows, no API keys, ollama up but without the model):
#:   guarded   ~3.05s   unguarded   ~9.36s
#: ~3.0s of that is `LiteLLMAdapter._call_model`'s tenacity backoff — `wait_exponential
#: (min=1)` x 1 retry x 3 models in the fallback chain — pure `asyncio.sleep` that the
#: guard cannot and should not remove. So 3.0s is the floor even on a perfect run, and
#: everything above it is network I/O. 6.0s sits at 2x the deterministic floor while
#: still failing on the *fastest* unguarded case observed; a real hang (reachable but
#: slow providers, up to 120s per model) blows past it by two orders of magnitude.
_LIMITE_SEGUNDOS = 6.0


async def test_el_adaptador_real_falla_rapido_en_vez_de_colgarse(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: the production adapter, unmocked, must fail immediately.

    This is the shape the hanging tests had — a real `get_llm()` call with no patch.
    It must surface `All LLM models failed` after nothing but the adapter's own
    deterministic backoff, instead of walking the 3-model chain over the network.
    """
    import time

    from app.adapters.llm import get_llm
    from app.core.config import settings
    from app.schemas.agent import LLMMessage

    # `get_llm()` reads the process-wide `settings` singleton, not a fresh `Settings()`
    # instance — force the real `LiteLLMAdapter` path regardless of a developer's local
    # `LLM_PROVIDER=fake` override (see `secrets/.env.local`, the sanctioned local-dev
    # workflow), otherwise this "network guard" test would silently exercise the
    # network-free `FakeLLMPort` instead and never hit the guard it exists to verify.
    monkeypatch.setattr(settings, "LLM_PROVIDER", "litellm")

    llm = get_llm()
    inicio = time.monotonic()
    with pytest.raises(RuntimeError, match="All LLM models failed"):
        await llm.complete([LLMMessage(role="user", content="hola")])
    transcurrido = time.monotonic() - inicio

    assert transcurrido < _LIMITE_SEGUNDOS, (
        f"El adaptador tardó {transcurrido:.2f}s (límite {_LIMITE_SEGUNDOS}s): el guard de red "
        "no está interceptando litellm y la llamada salió a la red de verdad."
    )
