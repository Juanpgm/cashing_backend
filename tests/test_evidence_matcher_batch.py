"""evidence_matcher must classify all candidates for an obligation in ONE LLM call
(batched), not one call per candidate."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agent.nodes import evidence_matcher


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _CountingLLM:
    """Records how many times complete() is called and returns a fixed batch answer."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(self, messages, temperature=0.0, max_tokens=64) -> _FakeResp:
        self.calls += 1
        return _FakeResp(self.content)


@pytest.mark.asyncio
async def test_matcher_batches_one_call_per_obligation() -> None:
    fake = _CountingLLM("[1]")  # only the first (highest-score) candidate is relevant
    state = {
        "obligaciones_extraidas": [
            {"id": "ob1", "descripcion": "realizar informes tecnicos mensuales consultoria"}
        ],
        "evidence_raw": [
            {"id": "a", "content": "informes tecnicos mensuales realizados consultoria"},
            {"id": "b", "content": "informes administrativos presupuesto reunion"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    # One batched call for the single obligation, NOT one per candidate.
    assert fake.calls == 1
    matched = result["matched_evidence"]["ob1"]
    assert [e["id"] for e in matched] == ["a"]  # only candidate 1 kept


@pytest.mark.asyncio
async def test_matcher_empty_when_no_candidates() -> None:
    fake = _CountingLLM("[]")
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": "algo muy especifico xyz"}],
        "evidence_raw": [{"id": "a", "content": "contenido totalmente distinto sin relacion"}],
    }
    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    # No candidate passes the keyword filter → no LLM call at all.
    assert fake.calls == 0
    assert result["matched_evidence"]["ob1"] == []


@pytest.mark.asyncio
async def test_matcher_max_effort_fallback_when_zero_candidates_above_threshold() -> None:
    """An obligación with ZERO candidates clearing the 0.15 keyword threshold must
    not stay silently empty if there's at least some weak (score > 0) overlap:
    the top-3 weakly-overlapping candidates are still sent to the LLM."""
    fake = _CountingLLM("[1]")  # LLM confirms the single weak candidate is relevant
    state = {
        "obligaciones_extraidas": [
            {"id": "ob1", "descripcion": "supervisar cronograma presupuestal financiero trimestral"}
        ],
        "evidence_raw": [
            # Only ONE overlapping word ("cronograma") → below the 0.15 filter, but > 0.
            {"id": "a", "content": "reunión de cronograma general del equipo administrativo"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    # The weak candidate WAS sent to the LLM (max-effort), not silently dropped.
    assert fake.calls == 1
    assert [e["id"] for e in result["matched_evidence"]["ob1"]] == ["a"]


@pytest.mark.asyncio
async def test_matcher_llm_error_falls_back_to_keyword_threshold_not_all_false() -> None:
    """On LLM error, evidence_matcher must NOT fail fully closed (drop everything) —
    it falls back to accepting only candidates whose keyword overlap already clears
    a stricter deterministic bar (>= 0.30)."""

    class _RaisingLLM:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("llm down")

    ob_text = "elaborar informes tecnicos mensuales consultoria asesoria"
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [
            # High overlap (>= 0.30) — should be accepted by the fallback.
            {"id": "strong", "content": "informes tecnicos mensuales consultoria asesoria elaborados"},
            # Low overlap (< 0.30, but >= 0.15 so it still passes the initial filter) — must be rejected.
            {"id": "weak", "content": "informes generales sin relacion directa con nada mas del contrato"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=_RaisingLLM()):
        result = await evidence_matcher.evidence_matcher_node(state)

    matched_ids = [e["id"] for e in result["matched_evidence"]["ob1"]]
    assert "strong" in matched_ids
    assert "weak" not in matched_ids


@pytest.mark.asyncio
async def test_matcher_garbage_llm_output_does_not_accept_low_score_candidates() -> None:
    """Unparseable LLM output must fall back the same way as an exception — garbage
    output must NOT accept low-score candidates just because the LLM "answered"."""
    fake = _CountingLLM("no soy un array JSON")

    ob_text = "elaborar informes tecnicos mensuales consultoria asesoria"
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [
            {"id": "strong", "content": "informes tecnicos mensuales consultoria asesoria elaborados"},
            {"id": "weak", "content": "informes generales sin relacion directa con nada mas del contrato"},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    matched_ids = [e["id"] for e in result["matched_evidence"]["ob1"]]
    assert "strong" in matched_ids
    assert "weak" not in matched_ids
