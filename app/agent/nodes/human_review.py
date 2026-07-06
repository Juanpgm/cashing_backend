"""Human review node — HumanInterrupt-based HIL review (Phase 6)."""

from __future__ import annotations

import structlog

from app.agent.engine import HumanInterrupt
from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.human_review")


def _build_review_message(state: AgentState) -> str:
    """Compose a clear HIL message for the user."""
    # Reason 1: missing template / hil_reason from template_resolver
    if state.get("hil_reason"):
        return state["hil_reason"]

    # Reason 2: extraction confidence < 0.7
    quality_passed = state.get("quality_gate_passed")
    quality_issues = state.get("quality_issues") or []
    if quality_passed is False and quality_issues:
        issues_text = "\n".join(f"  • {issue}" for issue in quality_issues[:5])
        return (
            "La extracción de obligaciones tiene baja confianza y requiere revisión:\n\n"
            f"{issues_text}\n\n"
            "¿Deseas proceder de todas formas o subir un documento más claro?\n\n"
            "Responde:\n"
            "• `continuar` — Usar las obligaciones extraídas tal como están\n"
            "• `subir` — Subir un documento mejor\n"
            "• `editar` — Editar las obligaciones manualmente"
        )

    # Reason 3: final review before PDF generation
    drafts = state.get("document_drafts") or []
    if drafts and state.get("preview_approved") is None:
        draft_preview = drafts[0].get("content", "")[:500] if drafts else ""
        return (
            "Tu documento está listo para generarse como PDF. Por favor revisa el borrador:\n\n"
            f"---\n{draft_preview}...\n---\n\n"
            "¿Apruebas la generación del PDF?\n\n"
            "Responde:\n"
            "• `aprobar` — Generar PDF y organizar archivos\n"
            "• `editar` — Editar el borrador antes de generar\n"
            "• `cancelar` — Cancelar la operación"
        )

    # Generic review
    return (
        "Se requiere tu revisión antes de continuar.\n\n"
        "Responde `continuar` para proceder o describe los cambios que necesitas."
    )


async def human_review_node(state: AgentState) -> AgentState:
    """Pause for human review at critical decision points.

    HIL trigger points:
    1. hil_reason is set (from template_resolver)
    2. quality_gate_passed is False
    3. preview_approved is None and document_drafts exist

    When hil_feedback is None: raises HumanInterrupt to pause graph.
    When hil_feedback is present: consumes it and continues.

    Reads: hil_reason, quality_gate_passed, document_drafts, hil_feedback
    Writes: preview_approved, human_review_pending (cleared), current_phase, hil_feedback (cleared)
    """
    # Check if we have feedback from a resume
    user_response: str | None = state.get("hil_feedback")

    if user_response is None:
        # First pass — pause and wait for human input
        message = _build_review_message(state)
        await logger.ainfo("human_review_interrupt", reason=message[:100])
        raise HumanInterrupt(message)

    # Resume pass — user_response contains the human's decision
    await logger.ainfo("human_review_resumed", response=user_response, hil_feedback=user_response)

    response_lower = (user_response or "").strip().lower()
    approved = response_lower in ("aprobar", "continuar", "si", "sí", "yes", "ok", "1")
    cancelled = response_lower in ("cancelar", "no", "cancel")

    return {
        **state,
        "hil_feedback": None,
        "preview_approved": approved if state.get("document_drafts") else None,
        "human_review_pending": False,
        "hil_reason": None,
        "response": (
            "Perfecto, procediendo con la generación del PDF." if approved
            else "Operación cancelada. Puedes hacer ajustes y volver a intentarlo." if cancelled
            else f"Entendido: {user_response}. Puedo ayudarte a hacer esos cambios."
        ),
        "current_phase": "human_review",
    }
