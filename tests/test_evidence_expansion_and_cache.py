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
async def test_expansion_falls_back_when_the_llm_raises():
    from app.agent.nodes.query_expansion import expand_search_terms

    class _Boom:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("provider down")

    out = await expand_search_terms(
        {}, [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}], _Boom()
    )
    assert out["ob1"]


@pytest.mark.asyncio
async def test_expansion_falls_back_on_malformed_output():
    from app.agent.nodes.query_expansion import expand_search_terms

    class _Garbage:
        async def complete(self, messages, **kwargs):
            class _R:
                content = "lo siento, no puedo"

            return _R()

    out = await expand_search_terms(
        {}, [{"id": "ob1", "descripcion": "Elaborar informes mensuales de seguimiento"}], _Garbage()
    )
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
async def test_expansion_makes_at_most_one_llm_call_for_many_obligaciones():
    from app.agent.nodes.query_expansion import expand_search_terms

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
