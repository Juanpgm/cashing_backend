"""Tests for semantic query-term expansion (evidencias/discovery-fix WU7).

Root cause: the search was too literal — emails/Drive files/Meet meetings that
satisfy an obligación without ever mentioning the contract number (e.g. a
supervisor's reply titled "Re: avance de actividades" instead of quoting the
contract) were never found. `expand_search_terms` asks one LLM call for 4-8
search phrases per obligación (synonyms, deliverable names, counterpart names,
filename variants), with a deterministic fallback to `_extract_keywords` when
the LLM is unavailable/fake/malformed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.agent.nodes.query_expansion import expand_search_terms
from app.core.config import settings


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


@pytest.mark.asyncio
async def test_no_obligaciones_returns_empty_dict() -> None:
    assert await expand_search_terms({}, [], MagicMock()) == {}


@pytest.mark.asyncio
async def test_llm_none_falls_back_to_deterministic_keywords() -> None:
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    result = await expand_search_terms({}, obligaciones, None)

    assert "ob1" in result
    assert len(result["ob1"]) > 0


@pytest.mark.asyncio
async def test_deterministic_fallback_produces_multi_word_phrases() -> None:
    """WARNING regression (escalated from SUGGESTION): the deterministic
    fallback used to emit the SAME single keywords `build_obligation_queries`
    already covers via `subject:(a OR b OR c)`. Wrapped individually in quotes
    by `build_expanded_phrase_queries`, each one became a near-useless
    unscoped full-text query that burned a guaranteed round-robin slot. Now
    the fallback emits multi-word phrases so they survive
    `build_expanded_phrase_queries`'s single-word filter and add real
    discriminating power instead of duplicating the subject: query."""
    from app.agent.prompts.email_evidence import build_expanded_phrase_queries

    obligaciones = [{"id": "ob1", "descripcion": "Elaborar informes de gestion mensual sobre arborizacion urbana"}]
    result = await expand_search_terms({}, obligaciones, None)

    assert all(" " in phrase for phrase in result["ob1"]), f"expected multi-word phrases, got: {result['ob1']}"
    queries = build_expanded_phrase_queries(result["ob1"], "2024/04/01", "2024/04/30")
    assert queries, "deterministic fallback phrases were all filtered out as single words"


@pytest.mark.asyncio
async def test_well_formed_llm_response_returns_phrases_per_obligacion(monkeypatch) -> None:
    # Expansion skips the LLM round trip when the configured model has no
    # credentials (an unauthenticated call burns tenacity's retry/backoff chain
    # for nothing). Stub one so the LLM path under test actually runs.
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")
    obligaciones = [
        {"id": "ob1", "descripcion": "Entregar informe mensual de actividades"},
        {"id": "ob2", "descripcion": "Asistir a reuniones de seguimiento"},
    ]
    llm = AsyncMock()
    llm.complete = AsyncMock(
        return_value=_FakeResp(
            '{"ob1": ["informe de avance", "reporte mensual", "monthly report", "acta de entrega"], '
            '"ob2": ["acta de reunión", "listado de asistencia", "meeting minutes", "minuta"]}'
        )
    )

    result = await expand_search_terms({"entidad": "DAGMA"}, obligaciones, llm)

    assert 4 <= len(result["ob1"]) <= 8
    assert "informe de avance" in result["ob1"]
    assert "acta de reunión" in result["ob2"]


@pytest.mark.asyncio
async def test_malformed_llm_response_falls_back_to_deterministic_keywords(monkeypatch) -> None:
    # WARNING regression: without this stub the credentials guard skips
    # llm.complete entirely, so this test passed vacuously — it never actually
    # exercised the malformed-response fallback it claims to cover.
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp("no soy json en absoluto"))

    result = await expand_search_terms({}, obligaciones, llm)

    llm.complete.assert_awaited_once()
    assert "ob1" in result
    assert len(result["ob1"]) > 0


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_deterministic_keywords(monkeypatch) -> None:
    # WARNING regression: see test_malformed_llm_response_falls_back_to_deterministic_keywords.
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=RuntimeError("boom"))

    result = await expand_search_terms({}, obligaciones, llm)

    llm.complete.assert_awaited_once()
    assert "ob1" in result
    assert len(result["ob1"]) > 0


@pytest.mark.asyncio
async def test_llm_response_missing_an_obligacion_still_gets_fallback_for_it(monkeypatch) -> None:
    """The LLM only answered for ob1 — ob2 must still get its deterministic
    fallback terms instead of being silently dropped.

    WARNING regression: see test_malformed_llm_response_falls_back_to_deterministic_keywords —
    without the credentials stub this never actually called the LLM.
    """
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")
    obligaciones = [
        {"id": "ob1", "descripcion": "Entregar informe mensual de actividades"},
        {"id": "ob2", "descripcion": "Asistir a reuniones de seguimiento del proyecto"},
    ]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["informe de avance", "reporte", "avance mensual", "x"]}'))

    result = await expand_search_terms({}, obligaciones, llm)

    llm.complete.assert_awaited_once()
    assert "ob1" in result and "ob2" in result
    assert len(result["ob2"]) > 0


@pytest.mark.asyncio
async def test_contexto_usuario_included_as_primary_hint_in_prompt(monkeypatch) -> None:
    """evidencias/discovery-fix WU7b: the contratista's own monthly summary
    ("¿Qué hiciste este mes?") must reach the expansion prompt as the primary
    hint of what was actually done this period."""
    # Expansion skips the LLM round trip when the configured model has no
    # credentials (an unauthenticated call burns tenacity's retry/backoff chain
    # for nothing). Stub one so the LLM path under test actually runs.
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["a", "b", "c", "d"]}'))

    await expand_search_terms(
        {}, obligaciones, llm, contexto_usuario="Entregué el informe y asistí a 2 reuniones con el supervisor"
    )

    prompt = llm.complete.call_args.args[0][1].content
    assert "Entregué el informe y asistí a 2 reuniones con el supervisor" in prompt


@pytest.mark.asyncio
async def test_contexto_usuario_adds_derived_phrases_without_llm() -> None:
    """Fallback path (llm=None): 2-4 phrases derived from contexto_usuario
    must still be added — this is deterministic, not LLM-dependent."""
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}]

    result = await expand_search_terms(
        {}, obligaciones, None, contexto_usuario="Coordiné la logística del evento anual con proveedores externos"
    )

    assert any(term in result["ob1"] for term in ("coordiné", "logística", "evento", "proveedores"))


@pytest.mark.asyncio
async def test_contexto_usuario_phrases_merged_even_when_llm_ignores_them(monkeypatch) -> None:
    """Phrases derived from contexto_usuario must be present in the final
    result even when the LLM's own answer doesn't mention them at all —
    this is an unconditional addition, not a suggestion the LLM can drop.

    WARNING regression: without this stub the credentials guard skips
    llm.complete entirely, so the LLM's answer never actually participates —
    the merge logic under test was never exercised."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["informe de avance", "reporte", "x", "y"]}'))

    result = await expand_search_terms({}, obligaciones, llm, contexto_usuario="Coordiné la logística del evento anual")

    llm.complete.assert_awaited_once()
    assert any(term in result["ob1"] for term in ("coordiné", "logística", "evento"))


@pytest.mark.asyncio
async def test_no_contexto_usuario_behaves_exactly_as_before() -> None:
    """Fallback: empty/None contexto_usuario → behavior unchanged."""
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]

    without = await expand_search_terms({}, obligaciones, None)
    with_none = await expand_search_terms({}, obligaciones, None, contexto_usuario=None)
    with_empty = await expand_search_terms({}, obligaciones, None, contexto_usuario="")

    assert without == with_none == with_empty


@pytest.mark.asyncio
async def test_prompt_includes_contract_header(monkeypatch) -> None:
    # Expansion skips the LLM round trip when the configured model has no
    # credentials (an unauthenticated call burns tenacity's retry/backoff chain
    # for nothing). Stub one so the LLM path under test actually runs.
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["a", "b", "c", "d"]}'))

    await expand_search_terms({"numero_contrato": "4161.010.26.1.027.2025", "entidad": "DAGMA"}, obligaciones, llm)

    prompt = llm.complete.call_args.args[0][1].content
    assert "4161.010.26.1.027.2025" in prompt
    assert "DAGMA" in prompt


# ── Credentials guard: whole fallback chain, not just the primary ─────────────


@pytest.mark.asyncio
async def test_credentials_guard_checks_the_whole_fallback_chain_not_just_the_primary(monkeypatch) -> None:
    """WARNING regression: the guard checked ONLY
    settings.LLM_EVIDENCE_CLASSIFIER_MODEL, ignoring the REST of the fallback
    chain `LiteLLMAdapter._get_model_chain` would actually try. A deployment
    missing the primary provider's key but with a working Gemini fallback had
    expansion permanently OFF with no way to tell."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "sk-gemini-test")
    monkeypatch.setattr(settings, "LLM_FALLBACK_MODEL", "gemini/gemini-2.5-flash")
    monkeypatch.setattr(settings, "LLM_EVIDENCE_CLASSIFIER_MODEL", "groq/openai/gpt-oss-20b")

    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["informe de avance", "reporte", "x", "y"]}'))

    result = await expand_search_terms({}, obligaciones, llm)

    llm.complete.assert_awaited_once()
    assert "informe de avance" in result["ob1"]


@pytest.mark.asyncio
async def test_credentials_guard_does_not_treat_the_default_ollama_fallback_as_reachable(monkeypatch) -> None:
    """The chain-aware guard must NOT degrade into 'always True' just because
    LLM_FALLBACK_MODEL/LLM_LOCAL_MODEL default to an ollama address nothing is
    actually running — that would silently reintroduce the wasted tenacity
    retry/backoff latency the guard exists to avoid."""
    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    # LLM_FALLBACK_MODEL / LLM_LOCAL_MODEL keep their real defaults (ollama/...).

    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["a", "b", "c", "d"]}'))

    result = await expand_search_terms({}, obligaciones, llm)

    llm.complete.assert_not_awaited()
    assert "ob1" in result and result["ob1"]


@pytest.mark.asyncio
async def test_credentials_guard_logs_when_expansion_is_skipped(monkeypatch) -> None:
    """Both round-3 skeptics flagged the silent skip as the more valuable half
    to fix: `return fallback` had no logger call, so an operator could not
    tell "expansion ran" from "expansion opted out"."""
    import structlog

    monkeypatch.setattr(settings, "GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")

    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    llm = AsyncMock()

    with structlog.testing.capture_logs() as captured:
        await expand_search_terms({}, obligaciones, llm)

    skip_events = [e for e in captured if e.get("event") == "query_expansion_skipped"]
    assert skip_events, f"expected a queryable log when expansion is skipped for missing credentials, got: {captured}"


@pytest.mark.asyncio
async def test_llm_provider_fake_explicitly_skips_the_credentials_call(monkeypatch) -> None:
    """`settings.LLM_PROVIDER == "fake"` is checked explicitly, defense-in-depth
    alongside the `FakeLLMPort` duck-type check — a caller injecting some other
    test double while LLM_PROVIDER=fake must still get the fast deterministic
    fallback instead of paying for a call that can't produce this node's JSON
    contract."""
    monkeypatch.setattr(settings, "LLM_PROVIDER", "fake")
    monkeypatch.setattr(settings, "GROQ_API_KEY", "sk-test")  # credentials present but irrelevant

    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    llm = AsyncMock()  # NOT a FakeLLMPort instance
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["a", "b", "c", "d"]}'))

    result = await expand_search_terms({}, obligaciones, llm)

    llm.complete.assert_not_awaited()
    assert "ob1" in result and result["ob1"]
