"""Activities generation node — generates billing activities from contract obligations."""

from __future__ import annotations

import re

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.actividades import ACTIVIDADES_GENERATION_PROMPT
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.activities")

_ACTIVIDAD_RE = re.compile(r"^ACTIVIDAD\|(.+?)\|(.+?)\|(\d+)\s*$", re.MULTILINE)


def _parse_actividades(response: str) -> list[dict[str, str | int]]:
    """Parse pipe-delimited ACTIVIDAD lines into plain dicts."""
    result: list[dict[str, str | int]] = []
    for descripcion, justificacion, ob_num_str in _ACTIVIDAD_RE.findall(response):
        desc = descripcion.strip()[:2000]
        just = justificacion.strip()[:3000]
        if len(desc) >= 10:
            result.append({
                "descripcion": desc,
                "justificacion": just,
                "obligacion_orden": int(ob_num_str) - 1,  # 0-based index
            })
    return result


async def generate_activities_node(state: AgentState) -> AgentState:
    """Generate billing activities for each contract obligation.

    Reads: obligaciones_contexto, contrato_contexto, texto_contrato (optional), mes, anio
    Writes: actividades_generadas
    """
    obligaciones = state.get("obligaciones_contexto") or []
    contrato = state.get("contrato_contexto") or {}
    texto = state.get("texto_contrato") or ""
    mes = state.get("mes") or 0
    anio = state.get("anio") or 0

    if not obligaciones:
        return {
            **state,
            "actividades_generadas": [],
            "error": "obligaciones_contexto requerido para generar actividades",
        }

    # Build context block for the LLM
    contrato_block = (
        f"Número: {contrato.get('numero_contrato', '—')}\n"
        f"Entidad: {contrato.get('entidad', '—')}\n"
        f"Objeto: {contrato.get('objeto', '—')}\n"
        f"Período: {mes}/{anio}"
    )

    obligaciones_block = "\n".join(
        f"{i + 1}. {ob.get('descripcion', '')}"
        for i, ob in enumerate(obligaciones)
    )

    texto_block = texto[:3000] if texto else "(texto del contrato no disponible)"

    user_content = (
        f"## CONTRATO\n{contrato_block}\n\n"
        f"## OBLIGACIONES\n{obligaciones_block}\n\n"
        f"## EXTRACTO DEL CONTRATO\n{texto_block}\n\n"
        f"Genera una actividad por cada obligación para el período {mes}/{anio}."
    )

    llm = get_llm()
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=ACTIVIDADES_GENERATION_PROMPT),
                LLMMessage(role="user", content=user_content),
            ],
            temperature=0.3,
            max_tokens=4096,
        )
    except Exception as exc:
        await logger.aerror("activities_generation_failed", error=str(exc))
        return {
            **state,
            "actividades_generadas": [],
            "error": f"Error generando actividades: {exc}",
        }

    actividades = _parse_actividades(resp.content)
    await logger.ainfo(
        "activities_generated",
        total=len(actividades),
        obligaciones=len(obligaciones),
        tokens=resp.total_tokens,
    )

    return {**state, "actividades_generadas": actividades}
