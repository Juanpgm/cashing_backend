"""Evidence justify node — genera el texto de justificación por obligación + links de soporte.

Cierra el flujo de evidencias: toma matched_evidence (obligación → evidencias) y produce,
por cada obligación, un párrafo de justificación y la lista de evidencias con su link, listo
para montar la Cuenta de Cobro / Radicación.
"""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.actividad_generation import (
    ACTIVIDAD_JUSTIFICACION_SYSTEM_PROMPT,
    build_actividad_justificacion_prompt,
    format_actividades_previas,
    is_near_identical,
    parse_actividad_justificacion,
)
from app.agent.prompts.evidence_justification import format_evidencias_for_prompt
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


def _evidence_titles(evidencias: list[dict], limit: int = 3) -> list[str]:
    return [
        str(ev.get("title") or ev.get("subject") or ev.get("filename") or "(sin título)")
        for ev in evidencias[:limit]
    ]


def _deterministic_actividad(evidencias: list[dict]) -> str:
    """Texto de actividad determinístico — usado en cualquier ruta degradada.

    NUNCA hace referencia al texto de la obligación: se basa exclusivamente en el
    número y los títulos de las evidencias recolectadas.
    """
    if not evidencias:
        return "No se registraron evidencias para el período consultado."
    titulos = ", ".join(_evidence_titles(evidencias))
    return f"Actividades del período soportadas en {len(evidencias)} evidencia(s): {titulos}."


def _deterministic_justificacion(evidencias: list[dict]) -> str:
    if evidencias:
        return (
            f"Se evidencia el cumplimiento de la obligación con {len(evidencias)} "
            f"soporte(s) recolectado(s). Ver evidencias adjuntas."
        )
    return "No se encontraron evidencias para esta obligación en el período consultado."


async def _generate_actividad_justificacion(
    obligacion_texto: str,
    evidencias: list[dict],
    contrato_contexto: str = "",
    actividades_previas: list[str] | None = None,
) -> tuple[str, str]:
    """Pide al LLM la ACTIVIDAD + JUSTIFICACION. Degrada con texto determinístico si falla.

    Returns (actividad, justificacion) — nunca iguales, nunca el texto de la obligación.
    """
    llm = get_llm()
    prompt = build_actividad_justificacion_prompt(
        obligacion_texto,
        format_evidencias_for_prompt(evidencias),
        contrato_contexto=contrato_contexto,
        actividades_previas_texto=format_actividades_previas(actividades_previas or []),
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=ACTIVIDAD_JUSTIFICACION_SYSTEM_PROMPT),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.3,
            max_tokens=512,
        )
    except Exception as exc:
        await logger.awarning("justify_llm_failed", error=str(exc))
        return _deterministic_actividad(evidencias), _deterministic_justificacion(evidencias)

    parsed = parse_actividad_justificacion(resp.content)
    if parsed is None:
        # Modelo no siguió el formato estricto (frecuente en modelos chicos/locales):
        # se conserva su texto libre como justificación (compatibilidad hacia atrás)
        # y se deriva una actividad determinística que NUNCA repite la obligación.
        justificacion = resp.content.strip() or _deterministic_justificacion(evidencias)
        actividad = _deterministic_actividad(evidencias)
        return actividad, justificacion

    actividad, justificacion = parsed
    if is_near_identical(actividad, justificacion):
        # El modelo violó la regla de "textos distintos" — no persistir dos copias
        # del mismo texto; recae en la justificación determinística.
        justificacion = _deterministic_justificacion(evidencias)
    return actividad, justificacion


async def evidence_justify_node(state: AgentState) -> AgentState:
    """Genera actividad + justificación por obligación a partir de matched_evidence.

    Reads: matched_evidence, obligaciones_contexto (u obligaciones_extraidas),
           contrato_contexto, actividades_previas
    Writes: justificaciones, current_phase
    """
    matched: dict[str, list[dict]] = state.get("matched_evidence") or {}
    obligaciones = state.get("obligaciones_contexto") or state.get("obligaciones_extraidas") or []
    contrato = state.get("contrato_contexto") or {}
    contrato_contexto = ""
    if contrato:
        contrato_contexto = ", ".join(f"{k}: {v}" for k, v in contrato.items() if v)
    actividades_previas = state.get("actividades_previas") or []

    justificaciones: list[dict] = []
    for i, ob in enumerate(obligaciones):
        ob_id = str(ob.get("id")) if isinstance(ob, dict) and ob.get("id") else str(i)
        ob_texto = _obligation_text(ob)
        evidencias = matched.get(ob_id, [])

        actividad, justificacion = await _generate_actividad_justificacion(
            ob_texto, evidencias, contrato_contexto, actividades_previas
        )
        justificaciones.append(
            {
                "obligacion_id": ob_id,
                "descripcion": ob_texto,
                "actividad": actividad,
                "justificacion": justificacion,
                "evidencias": _evidence_links(evidencias),
            }
        )

    await logger.ainfo("evidence_justify_done", obligaciones=len(justificaciones))
    return {
        **state,
        "justificaciones": justificaciones,
        "current_phase": "evidence_justify",
    }
