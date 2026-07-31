"""Tests for the embedding-based cosine ranking signal in `evidence_matcher_node`
(evidence-embeddings capability). Cosine similarity augments — never replaces —
keyword scoring, is computed once per run (not per file), and fails open to the
pre-existing keyword-only behavior whenever `embed()` errors.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from app.agent.nodes import evidence_matcher
from app.agent.nodes.evidence_matcher import _blended_score, _cosine_similarity, confidence_bucket
from app.core.config import settings

# Mixed sync (pure function) + async (node integration) tests in this file —
# no blanket `pytestmark`; `asyncio_mode = "auto"` (pyproject.toml) picks up the
# async ones without needing an explicit marker on either kind.


class _FakeResp:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeEmbedLLM:
    """Fake LLM exposing both `complete()` (batched relevance) and `embed()`.

    `embeddings` maps exact text -> vector; `embed_calls` records every batch
    passed to `embed()` so tests can assert reuse (called once per run, not once
    per evidence file).
    """

    def __init__(self, embeddings: dict[str, list[float]], relevance_content: str = "[1]") -> None:
        self.embeddings = embeddings
        self.relevance_content = relevance_content
        self.embed_calls: list[list[str]] = []
        self.complete_calls = 0

    async def complete(self, messages, temperature=0.0, max_tokens=64) -> _FakeResp:
        self.complete_calls += 1
        return _FakeResp(self.relevance_content)

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [self.embeddings[t] for t in texts]


# ── Pure function unit tests (cosine + blend) ──────────────────────────────────


class TestCosineSimilarity:
    def test_identical_vectors_return_one(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_return_zero(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_return_negative_one(self) -> None:
        assert _cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


class TestBlendedScore:
    def test_blend_prefers_cosine_when_higher(self) -> None:
        assert _blended_score(keyword_score=0.2, cosine_score=0.9) == pytest.approx(0.9)

    def test_blend_prefers_keyword_when_higher(self) -> None:
        assert _blended_score(keyword_score=0.5, cosine_score=0.1) == pytest.approx(0.5)

    def test_blend_falls_back_to_keyword_when_cosine_unavailable(self) -> None:
        assert _blended_score(keyword_score=0.3, cosine_score=None) == pytest.approx(0.3)


# ── Integration tests through evidence_matcher_node ────────────────────────────


async def test_cross_language_match_succeeds_via_embeddings() -> None:
    """A Spanish obligación and an English evidence text share zero keyword
    overlap, but the mocked embed() marks them as semantically identical — the
    blended score must clear the candidate threshold and produce a match."""
    ob_text = "desarrollar modulo de notificaciones por correo electronico"
    ev_text = "def send_email(to, subject, body): pass"

    fake = _FakeEmbedLLM(embeddings={ob_text: [1.0, 0.0], ev_text: [1.0, 0.0]}, relevance_content="[1]")
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [{"id": "a", "content": ev_text}],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    assert [e["id"] for e in result["matched_evidence"]["ob1"]] == ["a"]


async def test_dual_signal_evidence_outranks_keyword_only_within_top_n(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two evidence candidates clear the keyword filter for one obligación: ev1 has
    the HIGHER raw keyword score but zero semantic similarity, ev2 has a lower
    keyword score but high cosine similarity. With top_n=1, only the
    higher-BLENDED-score candidate should reach the LLM and end up matched."""
    monkeypatch.setattr(settings, "EVIDENCE_MATCHER_TOP_N", 1)

    ob_text = "elaborar informes tecnicos mensuales consultoria asesoria"
    ev1_text = "informes tecnicos mensuales consultoria asesoria elaborados"  # strong keyword overlap
    ev2_text = "informes tecnicos generales de consultoria"  # weaker keyword overlap, but cosine wins

    fake = _FakeEmbedLLM(
        embeddings={
            ob_text: [1.0, 0.0],
            ev1_text: [0.0, 1.0],  # orthogonal → cosine 0
            ev2_text: [1.0, 0.0],  # identical → cosine 1
        },
        relevance_content="[1]",
    )
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [
            {"id": "ev1", "content": ev1_text},
            {"id": "ev2", "content": ev2_text},
        ],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    matched_ids = [e["id"] for e in result["matched_evidence"]["ob1"]]
    assert matched_ids == ["ev2"]


async def test_embed_failure_falls_back_to_keyword_only_ranking() -> None:
    """embed() raising must NOT fail the run — classification completes using
    keyword-only ranking, exactly as before embeddings existed."""

    class _RaisingEmbedLLM:
        def __init__(self) -> None:
            self.complete_calls = 0

        async def complete(self, messages, temperature=0.0, max_tokens=64) -> _FakeResp:
            self.complete_calls += 1
            return _FakeResp("[1]")

        async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
            raise RuntimeError("embedding provider unreachable")

    ob_text = "realizar informes tecnicos mensuales consultoria"
    ev_text = "informes tecnicos mensuales realizados consultoria"
    fake = _RaisingEmbedLLM()
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [{"id": "a", "content": ev_text}],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    assert [e["id"] for e in result["matched_evidence"]["ob1"]] == ["a"]
    assert fake.complete_calls == 1


async def test_obligacion_embeddings_computed_once_per_run_across_many_files() -> None:
    """20 evidence files against 1 obligación must NOT trigger 20 embedding calls —
    obligación (and evidence) embeddings are batched once for the whole run."""
    ob_text = "supervisar cronograma presupuestal financiero trimestral"
    evidence_raw = [
        {"id": f"ev{i}", "content": "reunion de cronograma general del equipo administrativo"} for i in range(20)
    ]
    embeddings = {ob_text: [1.0, 0.0]}
    embeddings.update({ev["content"]: [1.0, 0.0] for ev in evidence_raw})
    # dedupe: all 20 evidence texts are identical, so the embed dict only needs one entry,
    # but embed() is called with the FULL list (20 entries) — assert call shape below.

    fake = _FakeEmbedLLM(embeddings=embeddings, relevance_content="[]")
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": evidence_raw,
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        await evidence_matcher.evidence_matcher_node(state)

    # Exactly 2 embed() calls total for the whole run: one obligación-text batch,
    # one evidence-text batch — never once per file.
    assert len(fake.embed_calls) == 2
    assert fake.embed_calls[0] == [ob_text]
    assert len(fake.embed_calls[1]) == 20


# ── Confidence bucketing (deferred from Phase 1/2, due in Phase 3 wiring) ──────
# evidence-obligation-links: Qualitative confidence levels


class TestConfidenceBucket:
    def test_alta_at_or_above_threshold(self) -> None:
        assert confidence_bucket(0.75) == "alta"
        assert confidence_bucket(0.9) == "alta"

    def test_media_between_thresholds(self) -> None:
        assert confidence_bucket(0.5) == "media"
        assert confidence_bucket(0.74) == "media"

    def test_baja_below_media_threshold(self) -> None:
        assert confidence_bucket(0.49) == "baja"
        assert confidence_bucket(0.0) == "baja"

    def test_respects_config_knobs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "EVIDENCE_EMBED_ALTA", 0.9)
        monkeypatch.setattr(settings, "EVIDENCE_EMBED_MEDIA", 0.6)
        assert confidence_bucket(0.85) == "media"  # would've been "alta" with defaults
        assert confidence_bucket(0.55) == "baja"


async def test_matched_evidence_scores_expose_blended_score_per_kept_match() -> None:
    """`evidence_matcher_node` must additively return the blended score behind each
    KEPT match — needed to bucket confidence when persisting evidencia_obligacion
    links (evidencia_service._clasificar_y_enlazar_lote consumes this)."""
    ob_text = "elaborar informes tecnicos mensuales consultoria asesoria"
    ev_text = "informes tecnicos mensuales consultoria asesoria elaborados"

    fake = _FakeEmbedLLM(embeddings={ob_text: [1.0, 0.0], ev_text: [1.0, 0.0]}, relevance_content="[1]")
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [{"id": "a", "content": ev_text}],
    }

    with patch.object(evidence_matcher, "get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    scores = result["matched_evidence_scores"]["ob1"]
    assert scores["a"] == pytest.approx(1.0)  # identical vectors + full keyword overlap
