"""Evidence matcher node — matches evidence to obligations via keyword + LLM (Phase 4)."""

from __future__ import annotations

import json
import re

import structlog

from app.adapters.llm import get_llm
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.evidence_matcher")

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


async def _llm_relevance_batch(obligation: str, evidences: list[str], llm) -> list[bool]:
    """Classify all candidate evidences for one obligation in a SINGLE LLM call.

    Returns a boolean per evidence (same order). Fails closed (all False) on error or
    unparseable output — consistent with the previous "if unsure, not relevant" rule.
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
            return [False] * len(evidences)
        nums = json.loads(match.group(0))
        relevant_idx = {int(n) for n in nums if isinstance(n, (int, float))}
        return [(i + 1) in relevant_idx for i in range(len(evidences))]
    except Exception:
        return [False] * len(evidences)


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

        # Step 2: LLM relevance on top-5 candidates — ONE batched call, not one per candidate
        if candidates:
            # Sort by keyword score descending
            candidates = sorted(
                candidates,
                key=lambda e: _keyword_score(ob_text, e.get("content", "")),
                reverse=True,
            )[:5]
            flags = await _llm_relevance_batch(
                ob_text, [ev.get("content", "") for ev in candidates], llm
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
