"""Matcher precision: the contract number is ONE signal, never the only one.

Round-2 regression suite for the confirmed CRITICAL findings about scoring and
matching in `app/agent/nodes/evidence_matcher.py`:

- the tokenizer scored Spanish stopwords and glued trailing punctuation, so
  unrelated prose scored EXACTLY as high as a real deliverable;
- `_contains_contract_number` used a bare substring test against a variant list
  containing the 4-digit dependency code, so `ORD-41612` "mentioned" the
  contract;
- an informe-like document with a number hit was auto-matched to EVERY
  obligación at score 1.0 with the LLM skipped entirely;
- the +0.4 number bonus crowded genuine semantic matches out of the TOP_N
  slate;
- a candidate pool of 41 returned ZERO matches where a pool of 40 returned 8.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

import pytest

DAGMA_NUMERO = "4161.010.26.1.027.2025"

OBLIGACION = "Elaborar informes mensuales de seguimiento a los proyectos"
REAL_EVIDENCE = "Adjunto el informe mensual de seguimiento de los proyectos para revision del supervisor"
NOISE_EVIDENCE = "Hola, te comparto los datos de la promocion que te mencione en la reunion de los socios"


# ── Tokenizer / keyword score ─────────────────────────────────────────────────


def test_keyword_score_ranks_real_evidence_above_unrelated_spanish_prose():
    """The whole point of keyword scoring. Both used to score 0.1667."""
    from app.agent.nodes.evidence_matcher import _keyword_score

    real = _keyword_score(OBLIGACION, REAL_EVIDENCE)
    noise = _keyword_score(OBLIGACION, NOISE_EVIDENCE)
    assert real > noise, f"real={real} noise={noise} — matcher cannot rank evidence above spam"


def test_keyword_score_ignores_spanish_stopwords():
    from app.agent.nodes.evidence_matcher import _keyword_score

    # Nothing but stopwords shared with the obligación.
    assert _keyword_score(OBLIGACION, "los del por que con una para este como") == 0.0


def test_keyword_score_is_not_broken_by_trailing_punctuation():
    """'informe.' must count as 'informe' — the glued token halved real matches."""
    from app.agent.nodes.evidence_matcher import _keyword_score

    with_dot = _keyword_score("Presentar informe de actividades", "Te envio el informe de actividades.")
    without = _keyword_score("Presentar informe de actividades", "Te envio el informe de actividades")
    assert with_dot == without
    assert with_dot > 0


def test_keyword_score_still_matches_the_contract_number_as_a_token():
    """Root cause #2 must stay fixed: a dotted number is a scoring token."""
    from app.agent.nodes.evidence_matcher import _keyword_score

    assert _keyword_score(DAGMA_NUMERO, f"Radicado {DAGMA_NUMERO} recibido") > 0


def test_noise_prose_stays_below_the_llm_failure_fallback_bar():
    """`_fallback_flags` auto-accepts at >= 0.30 when the LLM errors. Plain
    conversational Spanish scored 0.3846 and was emitted as evidence with no
    LLM verdict at all."""
    from app.agent.nodes.evidence_matcher import _FALLBACK_ACCEPT_THRESHOLD, _keyword_score

    ob = "Apoyar a la entidad en la elaboracion de los informes de gestion que sean requeridos por el supervisor"
    noise = "Hola como estas, te escribo para contarte que el fin de semana salimos con los del equipo"
    assert _keyword_score(ob, noise) < _FALLBACK_ACCEPT_THRESHOLD


async def test_llm_failure_fallback_uses_keyword_score_not_the_cosine_blended_score():
    """CRITICAL regression, end-to-end: `_fallback_flags`'s 0.30 bar is
    documented as keyword overlap, but production fed it the cosine-blended
    score (`_score` returns `max(keyword, cosine)`). Cosine similarity between
    two arbitrary Spanish texts is routinely well above 0.30, so on ANY
    relevance-LLM failure the whole TOP_N slate was accepted with ZERO keyword
    support whenever embeddings succeeded. Here the obligación and the evidence
    share no vocabulary at all (`_keyword_score` == 0.0), embeddings are faked
    to a high cosine, and `complete()` always raises — nothing must match."""
    from unittest.mock import patch

    from app.agent.nodes import evidence_matcher

    ob_text = "Elaborar informes mensuales de seguimiento a los proyectos asignados"
    ev_text = "Tu factura de Netflix esta disponible, revisa tu metodo de pago"
    assert evidence_matcher._keyword_score(ob_text, ev_text) == 0.0

    class _FailingCompleteHighCosineLLM:
        async def complete(self, messages, **kwargs):
            raise RuntimeError("provider 503")

        async def embed(self, texts, *, model=None):
            # Near-parallel vectors -> cosine ~1.0, exactly what real sentence
            # embeddings of unrelated Spanish text routinely produce.
            return [[1.0, 0.01] for _ in texts]

    fake = _FailingCompleteHighCosineLLM()
    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": ob_text}],
        "evidence_raw": [{"id": f"n{i}", "content": ev_text} for i in range(20)],
    }
    with patch("app.agent.nodes.evidence_matcher.get_llm", return_value=fake):
        result = await evidence_matcher.evidence_matcher_node(state)

    assert result["matched_evidence"]["ob1"] == []


# ── Contract-number detection ─────────────────────────────────────────────────


def test_contains_contract_number_requires_word_boundaries():
    """`ORD-41612` embeds '4161' — a substring test called that a contract hit."""
    from app.agent.nodes.evidence_matcher import _contains_contract_number
    from app.agent.prompts.contract_terms import contract_number_variants

    variants = contract_number_variants(DAGMA_NUMERO)
    ev = {"title": "Reporte semanal de tu pedido ORD-41612", "content": "Tu pedido ORD-41612 fue enviado"}
    assert _contains_contract_number(ev, variants) is False


def test_contains_contract_number_still_detects_the_real_number():
    from app.agent.nodes.evidence_matcher import _contains_contract_number
    from app.agent.prompts.contract_terms import contract_number_variants

    variants = contract_number_variants(DAGMA_NUMERO)
    assert _contains_contract_number({"title": f"Informe contrato {DAGMA_NUMERO}", "content": ""}, variants) is True
    # A normalized spelling still counts.
    assert _contains_contract_number({"title": "Informe 4161-010-26-1-027-2025", "content": ""}, variants) is True


def test_bare_dependency_code_alone_is_not_a_contract_hit():
    """'4161' is the ENTITY's dependency code, shared by every one of its
    contracts — on its own it must never mark evidence as this contract's."""
    from app.agent.nodes.evidence_matcher import _contains_contract_number
    from app.agent.prompts.contract_terms import contract_number_variants

    variants = contract_number_variants(DAGMA_NUMERO)
    assert _contains_contract_number({"title": "Factura numero 4161 de servicios", "content": ""}, variants) is False


def test_only_the_full_raw_number_grants_the_strong_bypass():
    from app.agent.nodes.evidence_matcher import _has_strong_contract_number
    from app.agent.prompts.contract_terms import contract_number_variants

    variants = contract_number_variants(DAGMA_NUMERO)
    assert _has_strong_contract_number({"title": f"Acta {DAGMA_NUMERO}", "content": ""}, variants) is True
    # A DIFFERENT contract of the same entity must not qualify.
    assert (
        _has_strong_contract_number({"title": "Informe final contrato 4161.010.26.1.099.2025", "content": ""}, variants)
        is False
    )


# ── Matcher node behaviour ────────────────────────────────────────────────────


class _CountingLLM:
    """Records relevance calls; answers 'nothing is relevant' so any match that
    appears in the result came from a BYPASS, not from a verdict."""

    def __init__(self, content: str = "[]") -> None:
        self.calls = 0
        self.batches: list[str] = []
        self._content = content

    async def complete(self, messages, **kwargs):
        self.calls += 1
        self.batches.append(messages[1].content)
        resp = MagicMock()
        resp.content = self._content
        return resp

    async def embed(self, texts):
        raise RuntimeError("embeddings unavailable")


@pytest.mark.asyncio
async def test_informe_like_document_does_not_auto_attach_to_every_obligacion(monkeypatch):
    """One Drive file matched ALL five unrelated obligaciones at 1.0, llm_calls=0."""
    from app.agent.nodes import evidence_matcher as mod

    fake = _CountingLLM()
    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: fake)

    state = {
        "obligaciones_extraidas": [
            {"id": f"ob{i}", "descripcion": f"obligacion totalmente distinta numero {i} xyzzy"} for i in range(5)
        ],
        "evidence_raw": [
            {
                "id": "a",
                "source": "drive",
                "file_id": "f1",
                "title": f"Informe mensual {DAGMA_NUMERO} abril.pdf",
                "content": "Informe mensual de abril",
            }
        ],
        "contrato_contexto": {"numero_contrato": DAGMA_NUMERO},
    }

    result = await mod.evidence_matcher_node(state)

    assert fake.calls > 0, "the LLM was bypassed entirely — it must decide WHICH obligación a document covers"
    matched = {ob: [e["id"] for e in evs] for ob, evs in result["matched_evidence"].items()}
    assert all(not evs for evs in matched.values()), (
        f"LLM said nothing is relevant, yet the document was attached anyway: {matched}"
    )


@pytest.mark.asyncio
async def test_informe_like_document_is_guaranteed_a_slot_in_the_llm_slate(monkeypatch):
    """Removing the bypass must not remove the SIGNAL: a number-bearing
    deliverable still has to reach the LLM even against 50 higher-scoring items."""
    from app.agent.nodes import evidence_matcher as mod

    fake = _CountingLLM()
    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: fake)

    noise = [
        {"id": f"n{i}", "source": "email", "content": "informes mensuales seguimiento proyectos elaborar"}
        for i in range(50)
    ]
    target = {
        "id": "target",
        "source": "drive",
        "file_id": "f1",
        "title": f"Acta de entrega {DAGMA_NUMERO}.pdf",
        "content": "Acta de entrega",
    }

    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": OBLIGACION}],
        "evidence_raw": [*noise, target],
        "contrato_contexto": {"numero_contrato": DAGMA_NUMERO},
    }

    await mod.evidence_matcher_node(state)

    assert fake.calls == 1
    assert "Acta de entrega" in fake.batches[0], "the number-bearing deliverable never reached the LLM slate"


@pytest.mark.asyncio
async def test_genuine_semantic_match_is_not_crowded_out_by_number_bearing_noise(monkeypatch):
    """50 'factura numero 4161...' items filled all 8 LLM slots and the real
    semantic match never reached the model."""
    from app.agent.nodes import evidence_matcher as mod

    fake = _CountingLLM()
    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: fake)

    noise = [
        {"id": f"n{i}", "source": "email", "content": f"factura numero {DAGMA_NUMERO} de servicios varios item {i}"}
        for i in range(50)
    ]
    real = {"id": "real", "source": "email", "content": "seguimiento mensual de los proyectos entregado al supervisor"}

    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": OBLIGACION}],
        "evidence_raw": [*noise, real],
        "contrato_contexto": {"numero_contrato": DAGMA_NUMERO},
    }

    await mod.evidence_matcher_node(state)

    assert fake.calls == 1
    assert "seguimiento mensual de los proyectos" in fake.batches[0], (
        "the genuine semantic match was crowded out of the LLM slate by number-bearing noise"
    )


@pytest.mark.asyncio
async def test_score_zero_number_bearing_noise_never_displaces_a_higher_scoring_candidate(monkeypatch):
    """WARNING regression: the 3 reserved number-bearing slots had no score
    floor at all, so 3 score-0.0 invoices (bare contract-number mention, no
    semantic overlap) evicted 3 genuinely relevant candidates ranked #6-#8 by
    score. A priority-1 (bare mention) item with score 0.0 must never reserve
    a slot that a strictly-higher-scoring candidate would otherwise take."""
    from app.agent.nodes import evidence_matcher as mod

    fake = _CountingLLM()
    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: fake)

    ob_keywords = ["informes", "mensuales", "seguimiento", "proyectos", "elaborar"]
    # 10 DISTINCT semantic candidates (a unique marker keeps them
    # distinguishable in the rendered prompt) with descending keyword overlap:
    # counts=[5,5,4,4,3,3,2,2,1,1] -> scores [1.0,1.0,.8,.8,.6,.6,.4,.4,.2,.2].
    word_counts = [5, 5, 4, 4, 3, 3, 2, 2, 1, 1]
    semantic = [
        {
            "id": f"s{i}",
            "source": "email",
            "content": " ".join([*ob_keywords[:n], f"marcador{i}"]),
        }
        for i, n in enumerate(word_counts)
    ]
    # 3 score-0.0 invoices that only carry the bare contract number.
    invoices = [
        {"id": f"inv{i}", "source": "email", "content": f"factura numero {DAGMA_NUMERO} de servicios varios {i}"}
        for i in range(3)
    ]

    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": OBLIGACION}],
        "evidence_raw": [*semantic, *invoices],
        "contrato_contexto": {"numero_contrato": DAGMA_NUMERO},
    }

    await mod.evidence_matcher_node(state)

    assert fake.calls == 1
    slate = fake.batches[0]
    # Top 8 semantic candidates by score (all > 0) must survive — the invoices
    # carry zero keyword overlap with OBLIGACION and must never displace them.
    for i in range(8):
        marker = f"marcador{i}"
        assert marker in slate, f"a positive-scoring semantic candidate ({marker}) was crowded out"


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_size", [39, 40, 41, 60])
async def test_candidate_pool_never_collapses_to_zero_matches(monkeypatch, pool_size):
    """One extra item flipped an obligación from 8 matches to 0: above
    EVIDENCE_MAX_CANDIDATES_FOR_LLM the 0.15 pre-gate was re-applied and an
    all-zero pool produced an EMPTY candidate list, so the LLM was never called.
    Selection must be rank-based, never threshold-to-empty."""
    from app.agent.nodes import evidence_matcher as mod

    fake = _CountingLLM(content='[{"idx": 1, "relevante": true, "score": 0.9, "razon": "x"}]')
    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: fake)

    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": "asesoria juridica especializada"}],
        "evidence_raw": [
            {"id": f"e{i}", "source": "email", "content": "quarterly budget spreadsheet attachment"}
            for i in range(pool_size)
        ],
    }

    await mod.evidence_matcher_node(state)

    assert fake.calls == 1, f"pool={pool_size}: the LLM was never called — obligación silently returned zero matches"


@pytest.mark.asyncio
async def test_llm_slate_is_still_bounded_by_top_n(monkeypatch):
    """Rank-based selection must not become an unbounded fan-out."""
    from app.agent.nodes import evidence_matcher as mod
    from app.core.config import settings

    fake = _CountingLLM()
    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: fake)

    state = {
        "obligaciones_extraidas": [{"id": "ob1", "descripcion": OBLIGACION}],
        "evidence_raw": [
            {"id": f"e{i}", "source": "email", "content": "informes mensuales seguimiento proyectos"} for i in range(60)
        ],
    }

    await mod.evidence_matcher_node(state)

    listed = re.findall(r"^\d+\. ", fake.batches[0], flags=re.M)
    assert len(listed) <= settings.EVIDENCE_MATCHER_TOP_N


# ── Embedding input ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_long_contract_objeto_is_truncated_before_being_embedded(monkeypatch):
    """An untruncated 1500-char objeto prefixed to EVERY obligación dominates
    the pooled vector, so every obligación embeds to nearly the same point."""
    from app.agent.nodes import evidence_matcher as mod
    from app.agent.prompts.contract_terms import OBJETO_EMBED_MAX_CHARS

    captured: list[list[str]] = []

    class _EmbedSpy:
        async def embed(self, texts):
            captured.append(list(texts))
            return [[1.0, 0.0] for _ in texts]

        async def complete(self, messages, **kwargs):
            resp = MagicMock()
            resp.content = "[]"
            return resp

    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: _EmbedSpy())

    objeto = "prestacion de servicios profesionales " * 60  # ~2200 chars
    state = {
        "obligaciones_extraidas": [
            {"id": "ob1", "descripcion": "Supervisar cronograma"},
            {"id": "ob2", "descripcion": "Elaborar informes"},
        ],
        "evidence_raw": [{"id": "e1", "source": "email", "content": "algo"}],
        "contrato_contexto": {"objeto": objeto},
    }

    await mod.evidence_matcher_node(state)

    ob_batch = captured[0]
    for text in ob_batch:
        assert len(text) <= OBJETO_EMBED_MAX_CHARS + 200, (
            f"obligación embedding input is {len(text)} chars — the objeto prefix was not truncated"
        )
    # The obligación's OWN wording must survive the truncation.
    assert any("cronograma" in t for t in ob_batch)
    assert any("informes" in t for t in ob_batch)
