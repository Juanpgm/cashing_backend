"""Document assembly node — generates document drafts from template + evidence (Phase 5)."""

from __future__ import annotations

import uuid

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.doc_assembly import (
    CUENTA_COBRO_USER,
    DOC_ASSEMBLY_SYSTEM,
    INFORME_ACTIVIDADES_USER,
)
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage
from app.services.fewshot_service import build_fewshot_prompt_section, get_fewshot_examples

logger = structlog.get_logger("agent.nodes.doc_assembly")


def _summarize_evidence(deduplicated: list[dict], max_items: int = 10) -> str:
    """Create a brief text summary of available evidence."""
    if not deduplicated:
        return "Sin evidencias disponibles."
    lines = []
    for ev in deduplicated[:max_items]:
        source = ev.get("source", "desconocido")
        if source == "email":
            lines.append(f"- Email: {ev.get('subject', 'Sin asunto')} ({ev.get('date', '')})")
        elif source == "local_file":
            lines.append(f"- Archivo: {ev.get('filename', 'Sin nombre')}")
        else:
            snippet = (ev.get("content") or "")[:100]
            lines.append(f"- {source}: {snippet}...")
    if len(deduplicated) > max_items:
        lines.append(f"  ... y {len(deduplicated) - max_items} evidencias más.")
    return "\n".join(lines)


def _summarize_obligations(obligaciones: list, max_items: int = 15) -> str:
    """Create a brief numbered list of obligations."""
    if not obligaciones:
        return "Sin obligaciones especificadas."
    lines = []
    for i, ob in enumerate(obligaciones[:max_items], 1):
        if isinstance(ob, dict):
            desc = ob.get("descripcion") or ob.get("texto") or str(ob)
        else:
            desc = str(ob)
        lines.append(f"{i}. {desc[:300]}")
    return "\n".join(lines)


async def doc_assembly_node(state: AgentState) -> AgentState:
    """Generate document drafts using LLM from contract + evidence + template.

    Reads: template_id, document_type, contrato_extraido, obligaciones_extraidas,
           deduplicated_evidence, mes, anio
    Writes: document_drafts, preview_html, current_phase
    """
    doc_type: str = state.get("document_type") or "cuenta_cobro"
    contrato: dict = state.get("contrato_extraido") or {}
    obligaciones: list = state.get("obligaciones_extraidas") or []
    evidencias: list[dict] = state.get("deduplicated_evidence") or state.get("evidence_raw") or []
    mes: int = state.get("mes") or 0
    anio: int = state.get("anio") or 0

    if not contrato:
        return {
            **state,
            "error": "contrato_extraido requerido para ensamblar documentos",
            "current_phase": "doc_assembly",
        }

    entidad = contrato.get("entidad") or contrato.get("nombre_entidad") or "Entidad"
    numero = contrato.get("numero_contrato") or "Sin número"
    objeto = contrato.get("objeto") or contrato.get("objeto_contrato") or ""
    valor = contrato.get("valor_mensual") or contrato.get("valor_total") or "0"
    contratista = contrato.get("contratista") or contrato.get("nombre_contratista") or "Contratista"

    evidencias_text = _summarize_evidence(evidencias)
    obligaciones_text = _summarize_obligations(obligaciones)

    # --- Few-shot context from previous approved drafts ---
    fewshot_section = ""
    usuario_id_raw = state.get("usuario_id")
    db = state.get("db")
    if db is not None and usuario_id_raw is not None:
        try:
            uid = uuid.UUID(str(usuario_id_raw)) if not isinstance(usuario_id_raw, uuid.UUID) else usuario_id_raw
            examples = await get_fewshot_examples(db, uid)
            fewshot_section = build_fewshot_prompt_section(examples)
        except Exception as _exc:
            await logger.awarning("fewshot_load_failed", error=str(_exc))

    system_prompt = DOC_ASSEMBLY_SYSTEM
    if fewshot_section:
        system_prompt = system_prompt + "\n\n" + fewshot_section

    llm = get_llm(model="gemini/gemini-2.5-flash")

    if doc_type == "informe_actividades":
        user_prompt = INFORME_ACTIVIDADES_USER.replace("{mes}", str(mes)).replace("{anio}", str(anio)).replace("{numero_contrato}", numero).replace("{entidad}", entidad).replace("{actividades}", obligaciones_text).replace("{evidencias_resumen}", evidencias_text)
    else:  # cuenta_cobro (default)
        user_prompt = CUENTA_COBRO_USER.replace("{mes}", str(mes)).replace("{anio}", str(anio)).replace("{nombre_contratista}", contratista).replace("{entidad}", entidad).replace("{numero_contrato}", numero).replace("{valor_mensual}", str(valor)).replace("{objeto}", objeto[:300]).replace("{obligaciones_cumplidas}", obligaciones_text).replace("{evidencias_resumen}", evidencias_text)

    messages = [
        LLMMessage(role="system", content=system_prompt),
        LLMMessage(role="user", content=user_prompt),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.3, max_tokens=3000)
        draft_text = resp.content
    except Exception as exc:
        await logger.awarning("doc_assembly_llm_failed", error=str(exc))
        draft_text = f"[Error generando {doc_type}: {exc}]"

    draft = {
        "type": doc_type,
        "content": draft_text,
        "mes": mes,
        "anio": anio,
        "numero_contrato": numero,
        "entidad": entidad,
    }

    # Simple HTML preview
    preview_html = (
        f"<html><body><pre style='font-family: monospace; padding: 2rem;'>"
        f"{draft_text.replace('<', '&lt;').replace('>', '&gt;')}"
        f"</pre></body></html>"
    )

    await logger.ainfo(
        "doc_assembly_done",
        doc_type=doc_type,
        draft_length=len(draft_text),
        n_evidencias=len(evidencias),
    )

    return {
        **state,
        "document_drafts": [draft],
        "preview_html": preview_html,
        "current_phase": "doc_assembly",
    }
