"""Supervisor node — plans and routes the CUENTA_COBRO_FULL pipeline (Phase 6)."""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.supervisor import SUPERVISOR_SYSTEM, SUPERVISOR_USER
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.supervisor")

# Valid node names the supervisor can route to
_VALID_NODES = {
    "obligations_extraction",
    "quality_gate",
    "evidence_orchestrator",
    "evidence_dedup",
    "doc_assembly",
    "folder_organizer",
    "human_review",
    "END",
}


def _determine_next_node(state: AgentState) -> str:
    """Deterministic fallback: inspect state fields to decide next step."""
    # Resume from saved plan if available
    plan = state.get("supervisor_plan") or []
    if plan:
        next_node = plan[0]
        if next_node in _VALID_NODES:
            return next_node

    # Otherwise derive from state
    if not state.get("obligaciones_extraidas"):
        return "obligations_extraction"
    if state.get("quality_gate_passed") is None:
        return "quality_gate"
    if not state.get("evidence_raw"):
        return "evidence_orchestrator"
    if not state.get("deduplicated_evidence"):
        return "evidence_dedup"
    if not state.get("document_drafts"):
        return "doc_assembly"
    if not state.get("folder_manifest"):
        return "folder_organizer"
    if state.get("preview_approved") is None:
        return "human_review"
    return "END"


async def supervisor_node(state: AgentState) -> AgentState:
    """Plan the CUENTA_COBRO_FULL pipeline execution.

    Reads: full state
    Writes: supervisor_plan, current_phase
    """
    # Try LLM-based planning first (with deterministic fallback)
    llm = get_llm(model="gemini/gemini-2.5-flash")

    context = {
        "tiene_obligaciones": "sí" if state.get("obligaciones_extraidas") else "no",
        "quality_passed": str(state.get("quality_gate_passed")),
        "tiene_evidencia": "sí" if state.get("evidence_raw") else "no",
        "tiene_evidencia_dedup": "sí" if state.get("deduplicated_evidence") else "no",
        "tiene_borradores": "sí" if state.get("document_drafts") else "no",
        "tiene_manifest": "sí" if state.get("folder_manifest") else "no",
        "preview_aprobado": str(state.get("preview_approved")),
        "hil_pendiente": str(state.get("human_review_pending")),
        "plan_actual": str(state.get("supervisor_plan") or []),
    }

    messages = [
        LLMMessage(role="system", content=SUPERVISOR_SYSTEM),
        LLMMessage(
            role="user",
            content=SUPERVISOR_USER.format(**context),
        ),
    ]

    next_node = _determine_next_node(state)  # deterministic fallback

    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=32)
        candidate = resp.content.strip().lower().replace('"', "").replace("'", "").split()[0] if resp.content.strip() else ""
        if candidate in _VALID_NODES:
            next_node = candidate
    except Exception as exc:
        await logger.awarning("supervisor_llm_failed", error=str(exc), fallback=next_node)

    # Pop front of plan if plan is in use
    plan = list(state.get("supervisor_plan") or [])
    if plan and plan[0] == next_node:
        plan = plan[1:]

    await logger.ainfo("supervisor_decision", next_node=next_node, remaining_plan=plan)

    return {
        **state,
        "supervisor_plan": [next_node] + plan if next_node != "END" else [],
        "current_phase": "supervisor",
    }
