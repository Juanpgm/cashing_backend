"""Tolerant parsing and defensive guards around the relevance LLM call.

Round-2 regression suite for three confirmed findings:

- WARNING: `_JSON_RE = r"\\[.*\\]"` is greedy across the whole response, so a
  reasoning preamble, a trailing sentence containing a bracket, or a
  max_tokens-truncated array all made the parser return None. The caller then
  fell through to the deterministic keyword bar, silently disabling the LLM
  noise filter the product depends on. `bool("false")` is also True, and a
  mixed array discarded every dict entry.
- WARNING: `score_ok = not isinstance(score, (int, float)) or score >= threshold`
  let a quoted "0.1" and an unnormalized 90 through while rejecting an honest
  0.4 — the threshold was bypassable by type.
- SUGGESTION: `_embed_batch` never checked that the provider returned one
  vector per input, so a short list raised IndexError out of an unguarded
  `asyncio.gather` and surfaced as a 500.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from app.agent.nodes.evidence_matcher import _parse_relevance_response

# ── Tolerant JSON extraction ──────────────────────────────────────────────────


def test_parses_a_bare_array():
    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": 0.9}]', 2) == [True, False]


def test_parses_a_fenced_json_block():
    raw = '```json\n[{"idx": 1, "relevante": true, "score": 0.9}]\n```'
    assert _parse_relevance_response(raw, 2) == [True, False]


def test_parses_an_array_followed_by_trailing_prose_with_brackets():
    raw = '[{"idx": 1, "relevante": true, "score": 0.9}]\nNota [1]: revisar el anexo.'
    assert _parse_relevance_response(raw, 2) == [True, False]


def test_parses_an_array_preceded_by_a_reasoning_preamble_with_brackets():
    """The preamble's numbers must NOT be mistaken for the verdict: this raw
    response has a preamble bracket list that DISAGREES with the structured
    array that follows it (round-2 CRITICAL regression — taking the FIRST
    balanced array returned the preamble `[2, 3]` as a legacy flat-index list,
    inverting the model's actual verdict)."""
    raw = "Analizo: los items [2, 3] parecen relevantes.\n" '[{"idx": 1, "relevante": true, "score": 0.9}]'
    assert _parse_relevance_response(raw, 3) == [True, False, False]


def test_extracts_the_last_object_array_when_multiple_bracket_runs_are_present():
    """Mirrors a realistic non-contrived model reply: 'Veo 2 items. El [2] no
    aplica.' followed by the real structured verdict — the bracketed mention in
    the prose must never be treated as the answer."""
    raw = 'Veo 2 items. El [2] no aplica.\n[{"idx":1,"relevante":true,"score":0.95},{"idx":2,"relevante":false}]'
    assert _parse_relevance_response(raw, 2) == [True, False]


def test_bare_int_array_is_only_accepted_when_no_object_array_exists_anywhere():
    """No object array anywhere in the text: the legacy flat-index format is the
    only signal available, so it must still be honored."""
    raw = "Analizo la lista.\n[1, 3]"
    assert _parse_relevance_response(raw, 3) == [True, False, True]


def test_recovers_the_complete_objects_from_a_truncated_array():
    """max_tokens=800 can cut the array mid-object; the complete prefix is still
    a usable verdict and must not be thrown away."""
    raw = '[{"idx": 1, "relevante": true, "score": 0.9, "razon": "ok"}, {"idx": 2, "relevante": tr'
    assert _parse_relevance_response(raw, 2) == [True, False]


def test_returns_none_only_when_there_is_no_parseable_json_at_all():
    assert _parse_relevance_response("No puedo responder.", 2) is None
    assert _parse_relevance_response("", 2) is None


def test_empty_array_means_nothing_is_relevant():
    assert _parse_relevance_response("[]", 3) == [False, False, False]


# ── `relevante` coercion ──────────────────────────────────────────────────────


@pytest.mark.parametrize("literal", ['"false"', '"False"', '"no"', '"0"', '""', "null", "0", "false"])
def test_falsy_relevante_values_are_not_relevant(literal):
    raw = f'[{{"idx": 1, "relevante": {literal}, "score": 0.9}}]'
    assert _parse_relevance_response(raw, 1) == [False]


@pytest.mark.parametrize("value", ['"true"', '"si"', '"sí"', '"1"', "true"])
def test_truthy_relevante_values_are_relevant(value):
    raw = f'[{{"idx": 1, "relevante": {value}, "score": 0.9}}]'
    assert _parse_relevance_response(raw, 1) == [True]


def test_mixed_array_is_handled_per_item_not_all_or_nothing():
    """A stray int used to send the WHOLE array down the legacy branch,
    discarding every dict entry."""
    raw = '[1, {"idx": 2, "relevante": true, "score": 0.9}]'
    assert _parse_relevance_response(raw, 2) == [True, True]


# ── Score threshold ───────────────────────────────────────────────────────────


def test_string_score_below_the_threshold_is_rejected():
    """A quoted "0.1" bypassed `isinstance(score, (int, float))` entirely."""
    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": "0.1"}]', 1) == [False]


def test_percentage_score_is_normalized_before_comparison():
    """An unnormalized 90 sailed through as 90 >= 0.5; and a 40 must not."""
    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": 90}]', 1) == [True]
    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": 40}]', 1) == [False]


def test_a_0_to_10_scale_score_does_not_fall_in_the_dead_zone():
    """SUGGESTION regression: `if value > 1.0: value /= 100.0` treats EVERY
    out-of-range score as a percentage, so a model drifting to a 0-10
    confidence scale (a common failure mode for small models, and the prompt
    only says "de 0 a 1") got silently divided by 100 too — e.g. an honest
    8/10 became 0.08 and a true `relevante: true` verdict was discarded with
    no recovery path. Values in the ambiguous (1, 10] range are now treated
    as unparseable/out-of-range and let `relevante` decide, exactly like an
    absent score — instead of confidently misinterpreting them as a
    percentage."""
    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": 8}]', 1) == [True]
    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": 1.5}]', 1) == [True]
    # A genuine relevante:false verdict must still stay false regardless of score.
    assert _parse_relevance_response('[{"idx": 1, "relevante": false, "score": 8}]', 1) == [False]


def test_null_score_is_treated_as_absent_not_as_passing():
    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": null}]', 1) == [True]


def test_absent_score_is_still_tolerated_for_legacy_models():
    """Documented intent: the score is checked "when present"."""
    assert _parse_relevance_response('[{"idx": 1, "relevante": true}]', 1) == [True]


def test_honest_low_score_is_still_rejected_and_the_boundary_is_inclusive():
    from app.core.config import settings

    assert _parse_relevance_response('[{"idx": 1, "relevante": true, "score": 0.4}]', 1) == [False]
    raw = f'[{{"idx": 1, "relevante": true, "score": {settings.EVIDENCE_RELEVANCE_MIN}}}]'
    assert _parse_relevance_response(raw, 1) == [True]


# ── Embedding length guard ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_embed_batch_fails_open_when_the_provider_returns_too_few_vectors():
    """A short list made `_score` index out of range, raising IndexError out of
    an unguarded gather and surfacing as a 500."""
    from app.agent.nodes.evidence_matcher import _embed_batch

    class _Short:
        async def embed(self, texts):
            return [[1.0, 0.0]]  # one vector for N texts

    assert await _embed_batch(["a", "b", "c"], _Short()) is None


@pytest.mark.asyncio
async def test_matcher_survives_a_provider_returning_a_short_embedding_batch(monkeypatch):
    from app.agent.nodes import evidence_matcher as mod

    class _Short:
        async def embed(self, texts):
            return [[1.0, 0.0]]

        async def complete(self, messages, **kwargs):
            resp = MagicMock()
            resp.content = "[]"
            return resp

    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: _Short())

    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": "elaborar informes mensuales"}],
        "evidence_raw": [{"id": f"e{i}", "content": f"contenido {i}"} for i in range(5)],
    }
    result = await mod.evidence_matcher_node(state)
    assert result["matched_evidence"] == {"ob1": []}


@pytest.mark.asyncio
async def test_one_failing_obligacion_does_not_abort_the_whole_run(monkeypatch):
    """`asyncio.gather` without return_exceptions propagated a single
    obligación's failure out of the node as an unhandled 500."""
    from app.agent.nodes import evidence_matcher as mod

    original = mod._match_una_obligacion
    calls = {"n": 0}

    async def _flaky(ob_id, *args, **kwargs):
        calls["n"] += 1
        if ob_id == "ob1":
            raise RuntimeError("provider exploded")
        return await original(ob_id, *args, **kwargs)

    class _LLM:
        async def embed(self, texts):
            raise RuntimeError("no embeddings")

        async def complete(self, messages, **kwargs):
            resp = MagicMock()
            resp.content = '[{"idx": 1, "relevante": true, "score": 0.9}]'
            return resp

    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: _LLM())
    monkeypatch.setattr(mod, "_match_una_obligacion", _flaky)

    state = {
        "obligaciones_extraidas": [
            {"id": "ob1", "descripcion": "elaborar informes mensuales de seguimiento"},
            {"id": "ob2", "descripcion": "elaborar informes mensuales de seguimiento"},
        ],
        "evidence_raw": [{"id": "e1", "content": "informes mensuales de seguimiento entregados"}],
    }

    result = await mod.evidence_matcher_node(state)

    assert result["matched_evidence"]["ob1"] == []  # failed obligación degrades to empty
    assert [e["id"] for e in result["matched_evidence"]["ob2"]] == ["e1"]  # the rest still work
