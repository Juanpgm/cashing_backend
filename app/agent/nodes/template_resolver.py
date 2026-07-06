"""Template resolver node — finds or interrupts for missing template (Phase 2 HIL)."""

from __future__ import annotations

import uuid

import structlog

from app.agent.engine import HumanInterrupt
from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.template_resolver")

# Default template UUIDs per document type (used when no custom template exists)
_DEFAULT_TEMPLATES: dict[str, uuid.UUID] = {
    "cuenta_cobro": uuid.UUID("00000000-0000-4000-8000-000000000001"),
    "informe_actividades": uuid.UUID("00000000-0000-4000-8000-000000000002"),
    "anexo": uuid.UUID("00000000-0000-4000-8000-000000000003"),
}


async def template_resolver_node(state: AgentState) -> AgentState:
    """Resolve the template for the requested document type.

    Reads: entity_profile_id, document_type, hil_feedback
    Writes: template_id, hil_reason (if interrupted), current_phase, hil_feedback (cleared)

    HIL: Raises HumanInterrupt if document_type is unknown or not provided and hil_feedback is None.
    On resume: uses hil_feedback as document_type selection.
    """
    doc_type: str | None = state.get("document_type")
    entity_profile_id = state.get("entity_profile_id")

    # If document_type missing, check for feedback from a prior HIL pause
    if not doc_type:
        hil_feedback = state.get("hil_feedback")
        if hil_feedback is None:
            # First pass — pause and ask user
            hil_message = (
                "¿Qué tipo de documento necesitas generar?\n\n"
                "Opciones:\n"
                "• `cuenta_cobro` — Cuenta de cobro mensual\n"
                "• `informe_actividades` — Informe de actividades\n"
                "• `anexo` — Documento anexo\n\n"
                "Responde con el tipo exacto."
            )
            await logger.awarning("template_resolver_hil_no_doc_type", entity_profile_id=str(entity_profile_id))
            raise HumanInterrupt(hil_message)
        # Resume pass — use feedback as the document type
        doc_type = str(hil_feedback).strip().lower() or "cuenta_cobro"

    # Look up template — for now use default per type; a DB lookup could be added here
    template_id = _DEFAULT_TEMPLATES.get(doc_type)
    if not template_id:
        # Unknown type: check for feedback
        hil_feedback = state.get("hil_feedback")
        if hil_feedback is None:
            hil_message = (
                f"No reconozco el tipo de documento `{doc_type}`.\n\n"
                "Por favor elige uno de:\n"
                "• `cuenta_cobro`\n"
                "• `informe_actividades`\n"
                "• `anexo`"
            )
            await logger.awarning("template_resolver_hil_unknown_type", doc_type=doc_type)
            raise HumanInterrupt(hil_message)
        doc_type = str(hil_feedback).strip().lower() or "cuenta_cobro"
        template_id = _DEFAULT_TEMPLATES.get(doc_type, _DEFAULT_TEMPLATES["cuenta_cobro"])

    await logger.ainfo(
        "template_resolver_done",
        doc_type=doc_type,
        template_id=str(template_id),
        entity_profile_id=str(entity_profile_id),
    )

    return {
        **state,
        "template_id": template_id,
        "document_type": doc_type,
        "hil_reason": None,
        "hil_feedback": None,
        "current_phase": "template_resolver",
    }
