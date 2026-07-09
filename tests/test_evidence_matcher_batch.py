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
