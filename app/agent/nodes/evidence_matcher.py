"""Evidence matcher node — matches evidence to obligations via keyword + LLM (Phase 4)."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

import structlog

from app.adapters.llm import get_llm
from app.agent.state import AgentState
from app.core.config import settings
from app.schemas.agent import LLMMessage

if TYPE_CHECKING:
    from app.models.obligacion import Obligacion

logger = structlog.get_logger("agent.nodes.evidence_matcher")

# Keyword-overlap threshold used both as the initial candidate filter and as the
# conservative fallback when the LLM relevance call errors out or returns
# unparseable output (see `_llm_relevance_batch`).
_KEYWORD_THRESHOLD = 0.15
_FALLBACK_ACCEPT_THRESHOLD = 0.30

_RELEVANCE_BATCH_SYSTEM = """\
Eres un clasificador. Dada una obligación contractual y una lista numerada de evidencias, \
indica cuáles evidencias son RELEVANTES para demostrar el cumplimiento de esa obligación.

Responde SOLO con un array JSON de los números (empezando en 1) de las evidencias relevantes. \
Ejemplo: [1, 3]. Si ninguna es relevante, responde [].
"""

_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)

_CLASIFICAR_SYSTEM = """\
Eres un clasificador. Dada una evidencia y una lista numerada de obligaciones \
contractuales candidatas, indica cuál obligación describe MEJOR esa evidencia.

Responde SOLO con el número de la obligación, o 0 si NINGUNA obligación aplica \
realmente a esta evidencia. Ejemplo: 2.
"""
_NUMBER_RE = re.compile(r"\d+")


async def _clasificar_via_llm(
    texto_evidencia: str, obligaciones: list[Obligacion], llm, fallback: Obligacion | None
) -> Obligacion | None:
    """One LLM call: pick the best-matching obligación out of `obligaciones`, or None.

    `fallback` is what's returned on LLM error or unparseable output — the
    caller passes the strongest keyword-score candidate when at least one
    already cleared `_KEYWORD_THRESHOLD` (weak-but-real signal to fall back
    on), or `None` when nothing did (the LLM is the ONLY signal, so a failure
    there must not fabricate a match). An explicit "0" answer from the model
    (out of range) also falls through to `fallback`, so it naturally resolves
    to None in the zero-keyword-candidate case.
    """
    listado = "\n".join(f"{i + 1}. {ob.descripcion[:400]}" for i, ob in enumerate(obligaciones))
    prompt = (
        f"Evidencia:\n{texto_evidencia[:800]}\n\n"
        f"Obligaciones candidatas:\n{listado}\n\n"
        "¿Cuál obligación describe mejor esta evidencia? Responde solo el número, o 0 si ninguna aplica."
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=_CLASIFICAR_SYSTEM),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=8,
        )
        match = _NUMBER_RE.search(resp.content)
        if not match:
            return fallback
        idx = int(match.group(0)) - 1
        if 0 <= idx < len(obligaciones):
            return obligaciones[idx]
        return fallback
    except Exception:
        return fallback


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """In-memory cosine similarity between two embedding vectors (numpy, no persistence)."""
    import numpy as np

    va, vb = np.array(a, dtype=float), np.array(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _blended_score(keyword_score: float, cosine_score: float | None) -> float:
    """Combine keyword overlap and cosine similarity: embeddings AUGMENT, never
    replace, keyword scoring (evidence-embeddings: Keyword and cosine blended score).
    `cosine_score=None` means embeddings were unavailable this run (fail-open) —
    the blended score degrades to the keyword-only score, unchanged."""
    if cosine_score is None:
        return keyword_score
    return max(keyword_score, cosine_score)


def confidence_bucket(score: float) -> str:
    """Map a blended keyword+cosine score to a qualitative confidence tier —
    "alta" auto-confirms a link, "media"/"baja" persist as proposed
    (evidence-obligation-links: Qualitative confidence levels). Thresholds are
    the EVIDENCE_EMBED_ALTA/MEDIA knobs deferred from Phase 1/2, wired here as
    the first real consumer (Phase 3)."""
    if score >= settings.EVIDENCE_EMBED_ALTA:
        return "alta"
    if score >= settings.EVIDENCE_EMBED_MEDIA:
        return "media"
    return "baja"


async def _embed_batch(texts: list[str], llm) -> list[list[float]] | None:
    """Embed a batch of texts once; fail-open returns None on ANY error (including
    a test double / older LLMPort implementation without `embed()`), so the caller
    degrades to keyword-only ranking instead of failing the run
    (evidence-embeddings: Fail-open to keyword ranking)."""
    if not texts:
        return []
    try:
        return await llm.embed(texts)  # type: ignore[no-any-return]
    except Exception as exc:
        logger.warning("evidence_matcher_embed_failed", error=str(exc))
        return None


def _obligacion_text(ob: Any) -> str:
    """Extract the descripcion/texto text used for matching, from a dict/str/other obligación shape."""
    if isinstance(ob, dict):
        return ob.get("descripcion") or ob.get("texto") or json.dumps(ob, ensure_ascii=False)
    if isinstance(ob, str):
        return ob
    return str(ob)


def _keyword_score(obligation_text: str, evidence_text: str) -> float:
    """Simple keyword overlap score between obligation and evidence."""
    if not obligation_text or not evidence_text:
        return 0.0

    # Tokenize: lower + split on non-alphanumeric (including Spanish chars)
    ob_words = set(re.findall(r"[a-záéíóúñüA-ZÁÉÍÓÚÑÜ]{4,}", obligation_text.lower()))
    ev_words = set(re.findall(r"[a-záéíóúñüA-ZÁÉÍÓÚÑÜ]{4,}", evidence_text.lower()))

    if not ob_words:
        return 0.0
    overlap = ob_words & ev_words
    return len(overlap) / len(ob_words)


def _fallback_flags(keyword_scores: list[float] | None, n: int) -> list[bool]:
    """Conservative fallback when the LLM call errors or returns unparseable output.

    Rather than failing fully closed (dropping every candidate for the obligación —
    "minimum effort"), accept candidates whose keyword overlap already clears a
    stricter deterministic bar (>= 0.30, double the initial 0.15 filter). Garbage
    LLM output must NOT accept low-score candidates: if no keyword_scores were
    provided, or none clear the bar, this still returns all-False.
    """
    if not keyword_scores:
        return [False] * n
    return [score >= _FALLBACK_ACCEPT_THRESHOLD for score in keyword_scores]


async def _llm_relevance_batch(
    obligation: str, evidences: list[str], llm, keyword_scores: list[float] | None = None
) -> list[bool]:
    """Classify all candidate evidences for one obligation in a SINGLE LLM call.

    Returns a boolean per evidence (same order). On error or unparseable output,
    falls back to `_fallback_flags` (deterministic keyword-score bar) instead of
    dropping every candidate — see module docstring / `_FALLBACK_ACCEPT_THRESHOLD`.
    """
    if not evidences:
        return []

    listado = "\n".join(f"{i + 1}. {ev[:600]}" for i, ev in enumerate(evidences))
    prompt = (
        f"Obligación: {obligation[:500]}\n\n"
        f"Evidencias:\n{listado}\n\n"
        "¿Cuáles evidencias son relevantes? Responde solo el array JSON de números."
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=_RELEVANCE_BATCH_SYSTEM),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=64,
        )
        match = _JSON_RE.search(resp.content)
        if not match:
            return _fallback_flags(keyword_scores, len(evidences))
        nums = json.loads(match.group(0))
        relevant_idx = {int(n) for n in nums if isinstance(n, (int, float))}
        return [(i + 1) in relevant_idx for i in range(len(evidences))]
    except Exception:
        return _fallback_flags(keyword_scores, len(evidences))


async def clasificar_evidencia(texto_evidencia: str, obligaciones: list[Obligacion], llm=None) -> Obligacion | None:
    """Best-matching obligación for one evidence text, or None if nothing matches.

    Scores every obligación's `descripcion` against `texto_evidencia` with the same
    `_keyword_score` used by the batch matcher above, and keeps candidates that clear
    `_KEYWORD_THRESHOLD`. Exactly one -> returned directly, no LLM call needed. Two or
    more -> ONE LLM call picks the single best match, falling back to the highest
    keyword-score candidate on LLM failure or unparseable/out-of-range output.

    Zero candidates -> the evidence shares no vocabulary with any obligación's Spanish
    wording, which is common for source code/config/log files whose real content (what
    the code DOES) doesn't echo the contract's phrasing at all. Rather than giving up,
    ONE LLM call judges the evidence against ALL obligaciones semantically (mirrors
    `evidence_matcher_node`'s own zero-candidate fallback) — the model may answer "0"
    (none apply) so genuinely unrelated evidence still lands as unclassified instead of
    being forced onto an arbitrary obligación.
    """
    if not obligaciones or not texto_evidencia:
        return None

    scored = [(ob, _keyword_score(ob.descripcion, texto_evidencia)) for ob in obligaciones]
    candidates = [(ob, score) for ob, score in scored if score >= _KEYWORD_THRESHOLD]

    if llm is None:
        llm = get_llm(model="groq/llama-3.1-8b-instant")

    if not candidates:
        return await _clasificar_via_llm(texto_evidencia, obligaciones, llm, fallback=None)
    if len(candidates) == 1:
        return candidates[0][0]

    candidates.sort(key=lambda pair: pair[1], reverse=True)
    fallback = candidates[0][0]
    return await _clasificar_via_llm(texto_evidencia, [ob for ob, _score in candidates], llm, fallback=fallback)


async def evidence_matcher_node(state: AgentState) -> AgentState:
    """Match evidence to obligations using keyword score + LLM refinement.

    Reads: evidence_raw, obligaciones_extraidas
    Writes: matched_evidence, current_phase
    """
    evidence_raw: list[dict] = state.get("evidence_raw") or []
    obligaciones: list = state.get("obligaciones_extraidas") or []

    if not obligaciones or not evidence_raw:
        return {
            **state,
            "matched_evidence": {},
            "matched_evidence_scores": {},
            "current_phase": "evidence_matcher",
        }

    llm = get_llm(model="groq/llama-3.1-8b-instant")
    matched: dict[str, list[dict]] = {}
    matched_scores: dict[str, dict[str, float]] = {}

    # Embed once for the whole run (per-contrato reuse, not once per evidence file):
    # one batch call for every obligación text, one for every evidence text. Skips
    # the evidence batch entirely if the obligación batch already failed, and
    # fails open to None (keyword-only ranking) on any embedding error.
    ob_texts = [_obligacion_text(ob) for ob in obligaciones]
    ev_texts = [ev.get("content", "") for ev in evidence_raw]
    ob_embeddings = await _embed_batch(ob_texts, llm)
    ev_embeddings = await _embed_batch(ev_texts, llm) if ob_embeddings is not None else None

    for i, ob in enumerate(obligaciones):
        ob_text = ob_texts[i]
        ob_id = ob.get("id") if isinstance(ob, dict) else str(i)
        if not ob_id:
            ob_id = str(i)
        ob_vec = ob_embeddings[i] if ob_embeddings is not None else None

        def _score(
            idx: int,
            ev: dict,
            ob_text: str = ob_text,
            ob_vec: list[float] | None = ob_vec,
            ev_embeddings: list[list[float]] | None = ev_embeddings,
        ) -> float:
            kw = _keyword_score(ob_text, ev.get("content", ""))
            cos = None
            if ob_vec is not None and ev_embeddings is not None:
                cos = _cosine_similarity(ob_vec, ev_embeddings[idx])
            return _blended_score(kw, cos)

        # Step 1: blended-score filter (≥0.15 threshold) — cosine can surface a
        # candidate keyword scoring alone would miss entirely (cross-language match).
        scored_all = [(ev, _score(idx, ev)) for idx, ev in enumerate(evidence_raw)]
        candidates_scored = [(ev, s) for ev, s in scored_all if s >= _KEYWORD_THRESHOLD]

        # Max-effort fallback: an obligación with ZERO candidates above threshold
        # would otherwise stay silently empty. Instead, take its best candidates
        # with ANY positive score (score > 0) and still let the LLM judge them —
        # better an obligación gets a weak-but-checked candidate than none at all.
        if not candidates_scored:
            positive = [(ev, s) for ev, s in scored_all if s > 0]
            positive.sort(key=lambda pair: pair[1], reverse=True)
            candidates_scored = positive[:3]

        # Step 2: LLM relevance on top-N candidates — ONE batched call, not one per candidate
        if candidates_scored:
            candidates_scored = sorted(candidates_scored, key=lambda pair: pair[1], reverse=True)[
                : settings.EVIDENCE_MATCHER_TOP_N
            ]
            candidates = [ev for ev, _s in candidates_scored]
            blended_scores = [s for _ev, s in candidates_scored]
            flags = await _llm_relevance_batch(
                ob_text, [ev.get("content", "") for ev in candidates], llm, blended_scores
            )
            matched[str(ob_id)] = [ev for ev, keep in zip(candidates, flags) if keep]
            # Additive: the blended score behind each KEPT match, keyed by the
            # evidence's own "id" field (present for local-upload evidence_raw
            # dicts; simply absent/skipped for sources that don't set one, e.g.
            # Google discovery — those flows don't consume this key).
            matched_scores[str(ob_id)] = {
                ev["id"]: score
                for ev, score, keep in zip(candidates, blended_scores, flags, strict=True)
                if keep and "id" in ev
            }
        else:
            matched[str(ob_id)] = []
            matched_scores[str(ob_id)] = {}

    total_matched = sum(len(v) for v in matched.values())
    await logger.ainfo(
        "evidence_matcher_done",
        n_obligations=len(obligaciones),
        total_matched=total_matched,
    )

    return {
        **state,
        "matched_evidence": matched,
        "matched_evidence_scores": matched_scores,
        "current_phase": "evidence_matcher",
    }
