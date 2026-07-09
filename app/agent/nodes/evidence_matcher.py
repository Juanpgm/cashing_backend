"""Evidence matcher node — matches evidence to obligations via keyword + LLM (Phase 4)."""

from __future__ import annotations

import json
import re

import structlog

from app.adapters.llm import get_llm
from app.agent.state import AgentState
from app.core.config import settings
from app.schemas.agent import LLMMessage

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
            "current_phase": "evidence_matcher",
        }

    llm = get_llm(model="groq/llama-3.1-8b-instant")
    matched: dict[str, list[dict]] = {}

    for i, ob in enumerate(obligaciones):
        ob_text = ""
        if isinstance(ob, dict):
            ob_text = ob.get("descripcion") or ob.get("texto") or json.dumps(ob, ensure_ascii=False)
        elif isinstance(ob, str):
            ob_text = ob
        else:
            ob_text = str(ob)

        ob_id = ob.get("id") if isinstance(ob, dict) else str(i)
        if not ob_id:
            ob_id = str(i)

        # Step 1: keyword filter (≥0.15 threshold)
        candidates = [
            ev for ev in evidence_raw
            if _keyword_score(ob_text, ev.get("content", "")) >= _KEYWORD_THRESHOLD
        ]

        # Max-effort fallback: an obligación with ZERO candidates above threshold
        # would otherwise stay silently empty. Instead, take its best candidates
        # with ANY positive keyword overlap (score > 0) and still let the LLM
        # judge them — better an obligación gets a weak-but-checked candidate
        # than none at all.
        if not candidates:
            scored = [
                (ev, _keyword_score(ob_text, ev.get("content", "")))
                for ev in evidence_raw
            ]
            positive = [(ev, s) for ev, s in scored if s > 0]
            positive.sort(key=lambda pair: pair[1], reverse=True)
            candidates = [ev for ev, _ in positive[:3]]

        # Step 2: LLM relevance on top-N candidates — ONE batched call, not one per candidate
        if candidates:
            # Sort by keyword score descending
            candidates = sorted(
                candidates,
                key=lambda e: _keyword_score(ob_text, e.get("content", "")),
                reverse=True,
            )[: settings.EVIDENCE_MATCHER_TOP_N]
            keyword_scores = [_keyword_score(ob_text, ev.get("content", "")) for ev in candidates]
            flags = await _llm_relevance_batch(
                ob_text, [ev.get("content", "") for ev in candidates], llm, keyword_scores
            )
            matched[str(ob_id)] = [ev for ev, keep in zip(candidates, flags) if keep]
        else:
            matched[str(ob_id)] = []

    total_matched = sum(len(v) for v in matched.values())
    await logger.ainfo(
        "evidence_matcher_done",
        n_obligations=len(obligaciones),
        total_matched=total_matched,
    )

    return {
        **state,
        "matched_evidence": matched,
        "current_phase": "evidence_matcher",
    }
