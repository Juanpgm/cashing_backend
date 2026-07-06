"""Evidence justify node — genera el texto de justificación por obligación + links de soporte.

Cierra el flujo de evidencias: toma matched_evidence (obligación → evidencias) y produce,
por cada obligación, un párrafo de justificación y la lista de evidencias con su link, listo
para montar la Cuenta de Cobro / Radicación.
"""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.evidence_justification import (
    EVIDENCE_JUSTIFICATION_SYSTEM_PROMPT,
    build_justification_prompt,
    format_evidencias_for_prompt,
)
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.evidence_justify")


def _obligation_text(ob: dict | str) -> str:
    if isinstance(ob, dict):
        return str(ob.get("descripcion") or ob.get("texto") or "")
    return str(ob)


def _evidence_links(evidencias: list[dict]) -> list[dict]:
    """Proyecta las evidencias a la forma de salida (titulo + link + fecha + fuente)."""
    out = []
    for ev in evidencias:
        out.append(
            {
                "source": ev.get("source", ""),
                "titulo": ev.get("title") or ev.get("subject") or ev.get("filename") or "(sin título)",
                "link": ev.get("link", ""),
                "fecha": ev.get("date", ""),
            }
        )
    return out


async def _generate_text(obligacion_texto: str, evidencias: list[dict]) -> str:
    """Pide al LLM el párrafo de justificación. Degrada con un texto base si falla."""
    llm = get_llm()
    prompt = build_justification_prompt(obligacion_texto, format_evidencias_for_prompt(evidencias))
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=EVIDENCE_JUSTIFICATION_SYSTEM_PROMPT),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.3,
            max_tokens=512,
        )
        return resp.content.strip()
    except Exception as exc:
        await logger.awarning("justify_llm_failed", error=str(exc))
        if evidencias:
            return (
                f"Se evidencia el cumplimiento de la obligación con {len(evidencias)} "
                f"soporte(s) recolectado(s). Ver evidencias adjuntas."
            )
        return "No se encontraron evidencias para esta obligación en el período consultado."


async def evidence_justify_node(state: AgentState) -> AgentState:
    """Genera justificaciones por obligación a partir de matched_evidence.

    Reads: matched_evidence, obligaciones_contexto (u obligaciones_extraidas)
    Writes: justificaciones, current_phase
    """
    matched: dict[str, list[dict]] = state.get("matched_evidence") or {}
    obligaciones = state.get("obligaciones_contexto") or state.get("obligaciones_extraidas") or []

    justificaciones: list[dict] = []
    for i, ob in enumerate(obligaciones):
        ob_id = str(ob.get("id")) if isinstance(ob, dict) and ob.get("id") else str(i)
        ob_texto = _obligation_text(ob)
        evidencias = matched.get(ob_id, [])

        texto = await _generate_text(ob_texto, evidencias)
        justificaciones.append(
            {
                "obligacion_id": ob_id,
                "descripcion": ob_texto,
                "justificacion": texto,
                "evidencias": _evidence_links(evidencias),
            }
        )

    await logger.ainfo("evidence_justify_done", obligaciones=len(justificaciones))
    return {
        **state,
        "justificaciones": justificaciones,
        "current_phase": "evidence_justify",
    }
