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
async def test_well_formed_llm_response_returns_phrases_per_obligacion() -> None:
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
async def test_malformed_llm_response_falls_back_to_deterministic_keywords() -> None:
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp("no soy json en absoluto"))

    result = await expand_search_terms({}, obligaciones, llm)

    assert "ob1" in result
    assert len(result["ob1"]) > 0


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_deterministic_keywords() -> None:
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual de actividades del contrato"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=RuntimeError("boom"))

    result = await expand_search_terms({}, obligaciones, llm)

    assert "ob1" in result
    assert len(result["ob1"]) > 0


@pytest.mark.asyncio
async def test_llm_response_missing_an_obligacion_still_gets_fallback_for_it() -> None:
    """The LLM only answered for ob1 — ob2 must still get its deterministic
    fallback terms instead of being silently dropped."""
    obligaciones = [
        {"id": "ob1", "descripcion": "Entregar informe mensual de actividades"},
        {"id": "ob2", "descripcion": "Asistir a reuniones de seguimiento del proyecto"},
    ]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["informe de avance", "reporte", "avance mensual", "x"]}'))

    result = await expand_search_terms({}, obligaciones, llm)

    assert "ob1" in result and "ob2" in result
    assert len(result["ob2"]) > 0


@pytest.mark.asyncio
async def test_prompt_includes_contract_header() -> None:
    obligaciones = [{"id": "ob1", "descripcion": "Entregar informe mensual"}]
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=_FakeResp('{"ob1": ["a", "b", "c", "d"]}'))

    await expand_search_terms({"numero_contrato": "4161.010.26.1.027.2025", "entidad": "DAGMA"}, obligaciones, llm)

    prompt = llm.complete.call_args.args[0][1].content
    assert "4161.010.26.1.027.2025" in prompt
    assert "DAGMA" in prompt
