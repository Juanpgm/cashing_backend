"""Evidence filter node — descarta ruido antes del matching (trabajo vs. ruido, Phase 4b).

Corre entre evidence_orchestrator y evidence_matcher. Dos capas:
1. Heurísticas deterministas gratis (patrones de remitente/asunto, labels de Gmail,
   metadatos de Calendar, carpetas de Drive).
2. Clasificador LLM batch "TRABAJO/RUIDO" para los items que pasan la capa 1.

Default de agresividad: en caso de duda conserva el item (nunca pierde evidencia válida).
"""

from __future__ import annotations

import json
import re

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.evidence_filter import (
    WORK_NOISE_SYSTEM_PROMPT,
    build_work_noise_prompt,
    is_noise_calendar,
    is_noise_drive,
    score_non_personal_email,
)
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.evidence_filter")

_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)
_LLM_BATCH_SIZE = 15


async def _llm_classify_batch(items: list[dict], llm) -> list[bool]:  # True = TRABAJO
    """Clasifica un lote de items como TRABAJO o RUIDO vía LLM.

    En caso de error de LLM o parseo, conserva todos los items (safe default).
    Devuelve lista de booleans (True = conservar) con mismo índice que `items`.
    """
    if not items:
        return []

    indexed = [{"idx": i, "source": it["source"], "title": it["title"], "content": it["content"]} for i, it in enumerate(items)]
    prompt = build_work_noise_prompt(indexed)

    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=WORK_NOISE_SYSTEM_PROMPT),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            max_tokens=256,
        )
        raw = resp.content or ""
        m = _JSON_RE.search(raw)
        if not m:
            raise ValueError("No JSON array in LLM response")
        verdicts: list[dict] = json.loads(m.group())
        idx_to_verdict = {int(v["idx"]): v.get("verdict", "TRABAJO") for v in verdicts if isinstance(v, dict)}
    except Exception as exc:
        await logger.awarning("evidence_filter_llm_failed", error=str(exc), batch_size=len(items))
        # Conservar todo el lote si el LLM falla
        return [True] * len(items)

    return [idx_to_verdict.get(i, "TRABAJO") != "RUIDO" for i in range(len(items))]


def _heuristic_is_noise(item: dict) -> bool:
    """Capa 1: heurísticas deterministas por source. True = descartar."""
    source = item.get("source", "")
    meta = item.get("metadata") or {}

    if source == "email":
        sender = meta.get("sender") or ""
        labels = meta.get("labels") or []
        title = item.get("title") or ""
        headers = meta.get("headers") or {}
        score, _ = score_non_personal_email(sender, title, labels, headers)
        return score >= 3

    if source == "calendar":
        cal_meta = meta.get("metadata") or {}
        title = item.get("title") or ""
        return is_noise_calendar(title, cal_meta)

    if source == "drive":
        mime = meta.get("mime_type") or ""
        return is_noise_drive(mime)

    return False


async def evidence_filter_node(state: AgentState) -> AgentState:
    """Filtra ruido de evidence_raw antes de pasarlo al matcher.

    Reads: evidence_raw
    Writes: evidence_raw (filtrado), evidencias_descartadas
    """
    evidence_raw: list[dict] = state.get("evidence_raw") or []
    if not evidence_raw:
        return {**state, "evidencias_descartadas": 0, "current_phase": "evidence_filter"}

    # Capa 1: heurísticas deterministas
    after_heuristics: list[dict] = []
    heuristic_dropped = 0
    for item in evidence_raw:
        if _heuristic_is_noise(item):
            heuristic_dropped += 1
            await logger.adebug(
                "evidence_filter_heuristic_drop",
                source=item.get("source"),
                title=item.get("title", "")[:80],
            )
        else:
            after_heuristics.append(item)

    # Capa 2: clasificador LLM batch
    llm = get_llm(model="groq/llama-3.1-8b-instant")
    kept: list[dict] = []
    llm_dropped = 0

    for batch_start in range(0, len(after_heuristics), _LLM_BATCH_SIZE):
        batch = after_heuristics[batch_start : batch_start + _LLM_BATCH_SIZE]
        keep_flags = await _llm_classify_batch(batch, llm)
        for item, keep in zip(batch, keep_flags):
            if keep:
                kept.append(item)
            else:
                llm_dropped += 1
                await logger.adebug(
                    "evidence_filter_llm_drop",
                    source=item.get("source"),
                    title=item.get("title", "")[:80],
                )

    total_dropped = heuristic_dropped + llm_dropped
    await logger.ainfo(
        "evidence_filter_done",
        original=len(evidence_raw),
        kept=len(kept),
        dropped_heuristic=heuristic_dropped,
        dropped_llm=llm_dropped,
    )

    return {
        **state,
        "evidence_raw": kept,
        "evidencias_descartadas": total_dropped,
        "current_phase": "evidence_filter",
    }
