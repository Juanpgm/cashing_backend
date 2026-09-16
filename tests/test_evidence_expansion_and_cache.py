"""Semantic expansion ON by default, plus cache-key and obligación-key hygiene.

Round-2 work for the remaining confirmed findings and the product decision that
`EVIDENCE_QUERY_EXPANSION_ENABLED` must default to True — semantic search per
obligación is the product, and the flag was gating it off.

The flag's stated justification ("a large slice of the existing test suite
exercises this path WITHOUT mocking an LLM") was measured false: with it on,
exactly one test failed, and that test only asserted the flag was off. The
fail-open contract is what actually deserves pinning, so it is pinned here.

Also covered:
- SUGGESTION: the cache key excludes contexto_usuario and the contract-derived
  search inputs, so editing the monthly summary served a pre-edit result.
- SUGGESTION: `query_expansion` wrote keys as `str(ob["id"] or index)` while all
  three consumers read `str(ob["id"] or "")`, so an empty-id obligación was
  keyed "0" on write and "" on read.
"""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture(autouse=True)
def _force_real_llm_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's local `secrets/.env.local` may set `LLM_PROVIDER=fake` for
    cost-free manual testing. These tests exercise the real/litellm
    credentials-guard and LLM-call/fallback paths and must not silently
    short-circuit because of that environment-specific override. The one test
    that specifically wants fake-provider behavior re-overrides this itself."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "LLM_PROVIDER", "litellm")


# ── The default ───────────────────────────────────────────────────────────────


def test_query_expansion_is_enabled_by_default():
    from app.core.config import Settings

    assert Settings().EVIDENCE_QUERY_EXPANSION_ENABLED is True


# ── Fail-open contract (what the flag was really guarding) ────────────────────


@pytest.mark.asyncio
async def test_expansion_falls_back_to_deterministic_terms_when_llm_is_none():
    from app.agent.nodes.query_expansion import expand_search_terms

    obligaciones = [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}]
    out = await expand_search_terms({}, obligaciones, None)
    assert out["ob1"], "no search phrases at all — the query builders would get nothing"


@pytest.mark.asyncio
async def test_expansion_falls_back_when_the_llm_raises(monkeypatch):
    """WARNING regression: without a credentials stub the guard skips
    llm.complete entirely, so this test passed vacuously without ever
    exercising the exception-fallback path it claims to cover."""
    from app.core.config import settings
    from app.agent.nodes.query_expansion import expand_search_terms

    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")

    class _Boom:
        called = False

        async def complete(self, messages, **kwargs):
            self.called = True
            raise RuntimeError("provider down")

    boom = _Boom()
    out = await expand_search_terms({}, [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}], boom)
    assert boom.called, "llm.complete was never awaited — the credentials guard skipped the call under test"
    assert out["ob1"]


@pytest.mark.asyncio
async def test_expansion_falls_back_on_malformed_output(monkeypatch):
    """WARNING regression: see test_expansion_falls_back_when_the_llm_raises."""
    from app.core.config import settings
    from app.agent.nodes.query_expansion import expand_search_terms

    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")

    class _Garbage:
        called = False

        async def complete(self, messages, **kwargs):
            self.called = True

            class _R:
                content = "lo siento, no puedo"

            return _R()

    garbage = _Garbage()
    out = await expand_search_terms(
        {}, [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}], garbage
    )
    assert garbage.called, "llm.complete was never awaited — the credentials guard skipped the call under test"
    assert out["ob1"]


@pytest.mark.asyncio
async def test_expansion_is_silent_and_cheap_for_a_fake_provider():
    """A fake/unavailable provider must not cost an LLM round trip now that the
    flag is on by default and ~30 existing tests exercise this path unmocked."""
    from app.adapters.llm.fake_adapter import FakeLLMPort
    from app.agent.nodes.query_expansion import expand_search_terms

    calls = {"n": 0}
    fake = FakeLLMPort()
    original = fake.complete

    async def _counting(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    fake.complete = _counting  # type: ignore[method-assign]

    out = await expand_search_terms(
        {}, [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}], fake
    )
    assert calls["n"] == 0, "the fake provider was called — expansion must short-circuit it"
    assert out["ob1"]


@pytest.mark.asyncio
async def test_expansion_makes_at_most_one_llm_call_for_many_obligaciones(monkeypatch):
    """One call TOTAL, never one per obligación. Credentials are stubbed because
    expansion deliberately skips the round trip when the configured model has
    none (see the cost tests below)."""
    from app.agent.nodes.query_expansion import expand_search_terms
    from app.core.config import settings

    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")

    calls = {"n": 0}

    class _LLM:
        async def complete(self, messages, **kwargs):
            calls["n"] += 1

            class _R:
                content = "{}"

            return _R()

    obligaciones = [{"id": f"ob{i}", "descripcion": f"Elaborar el producto numero {i}"} for i in range(12)]
    await expand_search_terms({}, obligaciones, _LLM())
    assert calls["n"] == 1


# ── Obligación key ────────────────────────────────────────────────────────────


def test_obligacion_key_is_the_same_on_write_and_read():
    """query_expansion wrote `str(id or index)` and the consumers read
    `str(id or "")`, so an empty-id obligación was keyed "0" vs ""."""
    from app.agent.prompts.query_budget import obligacion_key

    obligaciones = [{"id": "", "descripcion": "a"}, {"id": "x", "descripcion": "b"}]
    assert [obligacion_key(ob, i) for i, ob in enumerate(obligaciones)] == ["0", "x"]


@pytest.mark.asyncio
async def test_expanded_phrases_survive_an_empty_obligacion_id():
    from app.agent.nodes import drive_fetch as mod
    from app.agent.prompts.query_budget import obligacion_key

    ob = {"id": "", "descripcion": "Elaborar informes mensuales"}
    key = obligacion_key(ob, 0)

    from unittest.mock import AsyncMock, MagicMock, patch

    adapter = MagicMock()
    adapter.search_files = AsyncMock(return_value=[])
    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {"fecha_inicio": "2025-09-01", "fecha_fin": "2025-09-30"},
        "obligaciones_contexto": [ob],
        "expanded_terms": {key: ["acta de comite de seguimiento"]},
    }
    with patch.object(mod, "DriveAdapter", return_value=adapter):
        await mod.drive_fetch_node(state)

    terms = [c.args[1].keywords[0] for c in adapter.search_files.call_args_list]
    assert "acta de comite de seguimiento" in terms


# ── Cache key ─────────────────────────────────────────────────────────────────


def test_cache_key_distinguishes_a_changed_contexto_usuario():
    from app.services import discovery_cache

    discovery_cache.clear()
    uid, cid = uuid.uuid4(), uuid.uuid4()
    sentinel = object()

    discovery_cache.store(uid, cid, "2025-09-01", "2025-09-30", sentinel, context_fingerprint="a")  # type: ignore[arg-type]
    assert discovery_cache.get_cached(uid, cid, "2025-09-01", "2025-09-30", context_fingerprint="a") is sentinel
    assert discovery_cache.get_cached(uid, cid, "2025-09-01", "2025-09-30", context_fingerprint="b") is None


def test_cache_key_distinguishes_changed_contract_fields():
    from app.services import discovery_cache

    discovery_cache.clear()
    uid, cid = uuid.uuid4(), uuid.uuid4()
    sentinel = object()

    fp_before = discovery_cache.context_fingerprint({"numero_contrato": "A-1"}, "hice X")
    fp_after = discovery_cache.context_fingerprint({"numero_contrato": "A-2"}, "hice X")
    assert fp_before != fp_after

    discovery_cache.store(uid, cid, "2025-09-01", "2025-09-30", sentinel, context_fingerprint=fp_before)  # type: ignore[arg-type]
    assert discovery_cache.get_cached(uid, cid, "2025-09-01", "2025-09-30", context_fingerprint=fp_after) is None


def test_context_fingerprint_is_stable_for_equal_input():
    from app.services import discovery_cache

    a = discovery_cache.context_fingerprint({"numero_contrato": "A-1", "entidad": "DAGMA"}, "hice X")
    b = discovery_cache.context_fingerprint({"entidad": "DAGMA", "numero_contrato": "A-1"}, "hice X")
    assert a == b


def test_context_fingerprint_handles_none_and_empty():
    from app.services import discovery_cache

    assert discovery_cache.context_fingerprint(None, None) == discovery_cache.context_fingerprint({}, "")


# ── Dead code ─────────────────────────────────────────────────────────────────


def test_contract_search_terms_is_gone():
    """It had no production caller and its docstring claimed to be "the shared
    vocabulary fed into the Gmail/Drive/Calendar query builders", which was
    false. The real vocabulary is contract_query_variants + the round-robin
    allocation."""
    from app.agent.prompts import contract_terms

    assert not hasattr(contract_terms, "contract_search_terms")


def test_entidad_search_term_strips_legal_suffixes():
    """`_normalize_entidad` was transitively dead (only reachable from
    contract_search_terms). Its behaviour is genuinely useful, so it is wired
    into the entidad query term instead of being deleted with its caller."""
    from app.agent.prompts.contract_terms import entidad_search_term

    assert entidad_search_term("Constructora Andina S.A.S.") == "Constructora Andina"
    assert entidad_search_term("Hospital Universitario E.S.E") == "Hospital Universitario"
    assert entidad_search_term("DAGMA") == "DAGMA"
    assert entidad_search_term(None) == ""


# ── Expansion must be CHEAP when the provider cannot answer ───────────────────
#
# Flipping EVIDENCE_QUERY_EXPANSION_ENABLED to True made one discovery test file
# go from 4.8s to 59.4s. Root cause: `LiteLLMAdapter._call_model` is wrapped in
# tenacity `@retry(stop_after_attempt(2), wait_exponential(min=1, max=4))` and
# walks a 3-model fallback chain, so ONE expansion call against an
# unauthenticated/unreachable provider burns ~3s of real `asyncio.sleep` before
# reaching the deterministic fallback. That is wasted latency on a user-facing
# "descubrir" click, not just a slow suite.


def test_expansion_is_skipped_when_the_model_has_no_credentials(monkeypatch):
    from app.agent.nodes.query_expansion import _model_credentials_ready
    from app.core.config import settings

    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    assert _model_credentials_ready("groq/openai/gpt-oss-20b") is False

    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-real")
    assert _model_credentials_ready("groq/openai/gpt-oss-20b") is True


def test_ollama_needs_no_api_key(monkeypatch):
    """An Ollama-only deployment must keep semantic expansion."""
    from app.agent.nodes.query_expansion import _model_credentials_ready

    assert _model_credentials_ready("ollama/llama3.1:8b") is True
    assert _model_credentials_ready("ollama_chat/llama3.1:8b") is True


def test_unknown_provider_is_attempted_rather_than_silently_skipped():
    from app.agent.nodes.query_expansion import _model_credentials_ready

    assert _model_credentials_ready("some-new-vendor/model-x") is True


@pytest.mark.asyncio
async def test_expansion_without_credentials_makes_no_llm_call(monkeypatch):
    from app.agent.nodes import query_expansion as mod
    from app.core.config import settings

    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "LLM_EVIDENCE_CLASSIFIER_MODEL", "groq/openai/gpt-oss-20b")

    calls = {"n": 0}

    class _LLM:
        async def complete(self, messages, **kwargs):
            calls["n"] += 1
            raise AssertionError("must not be called without credentials")

    out = await mod.expand_search_terms(
        {}, [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}], _LLM()
    )
    assert calls["n"] == 0
    assert out["ob1"], "deterministic fallback must still be returned"


@pytest.mark.asyncio
async def test_expansion_is_time_bounded_and_falls_back_on_timeout(monkeypatch):
    """Even WITH credentials, a hung provider must not stall a user click."""
    import asyncio

    from app.agent.nodes import query_expansion as mod
    from app.core.config import settings

    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-real")
    monkeypatch.setattr(settings, "EVIDENCE_QUERY_EXPANSION_TIMEOUT_SECONDS", 0.1)

    class _Hangs:
        async def complete(self, messages, **kwargs):
            await asyncio.sleep(30)

    started = asyncio.get_event_loop().time()
    out = await mod.expand_search_terms(
        {}, [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}], _Hangs()
    )
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed < 5, f"expansion was not time-bounded (took {elapsed:.1f}s)"
    assert out["ob1"], "deterministic fallback must still be returned"
