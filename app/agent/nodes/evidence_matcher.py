"""Evidence matcher node — matches evidence to obligations via keyword + LLM (Phase 4)."""

from __future__ import annotations

import json
import re

import structlog

from app.adapters.llm import get_llm
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.evidence_matcher")

_RELEVANCE_SYSTEM = """\
Eres un clasificador binario. Dado el texto de una evidencia y una obligación contractual, \
determina si la evidencia es RELEVANTE para demostrar el cumplimiento de esa obligación.

Responde SOLO con: RELEVANTE o NO_RELEVANTE
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


async def _llm_relevance(obligation: str, evidence: str, llm) -> bool:
    """Ask LLM if evidence is relevant for obligation."""
    prompt = (
        f"Obligación: {obligation[:500]}\n\n"
        f"Evidencia: {evidence[:1000]}\n\n"
        "¿Es la evidencia relevante para esta obligación?"
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=_RELEVANCE_SYSTEM),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=16,
        )
        return "RELEVANTE" in resp.content.upper() and "NO_RELEVANTE" not in resp.content.upper()
    except Exception:
        return False


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
            if _keyword_score(ob_text, ev.get("content", "")) >= 0.15
        ]

        # Step 2: LLM relevance on top-5 candidates
        if candidates:
            # Sort by keyword score descending
            candidates = sorted(
                candidates,
                key=lambda e: _keyword_score(ob_text, e.get("content", "")),
                reverse=True,
            )[:5]
            relevant = []
            for ev in candidates:
                is_rel = await _llm_relevance(ob_text, ev.get("content", ""), llm)
                if is_rel:
                    relevant.append(ev)
            matched[str(ob_id)] = relevant
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
