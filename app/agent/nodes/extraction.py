"""Extraction nodes — contract metadata and obligations extraction via LangGraph."""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.contrato_extraction import CONTRATO_EXTRACTION_SYSTEM, CONTRATO_EXTRACTION_USER
from app.agent.prompts.obligaciones import OBLIGACIONES_SYSTEM, OBLIGACIONES_USER
from app.agent.state import AgentState
from app.agent.tools.contract_parser import (
    MAX_CHUNK_CHARS as _MAX_CHUNK_CHARS,
)
from app.agent.tools.contract_parser import (
    extract_obligaciones_verbatim as _extract_obligaciones_verbatim,
)
from app.agent.tools.contract_parser import (
    extract_obligation_sections as _extract_obligation_sections,
)
from app.agent.tools.contract_parser import (
    parse_campos_structured as _parse_campos_structured,
)
from app.agent.tools.contract_parser import (
    parse_obligaciones_structured as _parse_obligaciones_structured,
)
from app.core.config import settings
from app.schemas.agent import ContratoCamposLLM, LLMMessage, ObligacionesLLMList

logger = structlog.get_logger("agent.nodes.extraction")


async def contract_metadata_node(state: AgentState) -> AgentState:
    """Extract contract metadata fields from raw contract text.

    Reads: texto_contrato
    Writes: contrato_extraido, extraction_avisos
    """
    texto = state.get("texto_contrato") or ""
    if not texto:
        return {**state, "error": "texto_contrato requerido para extracción de metadatos"}

    extraction_model = settings.LLM_EXTRACTION_MODEL or None
    llm = get_llm(model=extraction_model)
    chunk = texto[:_MAX_CHUNK_CHARS]

    messages = [
        LLMMessage(role="system", content=CONTRATO_EXTRACTION_SYSTEM),
        LLMMessage(role="user", content=CONTRATO_EXTRACTION_USER.replace("{texto_contrato}", chunk)),
    ]

    avisos: list[str] = list(state.get("extraction_avisos") or [])
    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=2048, response_format=ContratoCamposLLM)
    except Exception as exc:
        msg = f"Error extrayendo metadatos del contrato: {exc}"
        await logger.awarning("contract_metadata_llm_failed", error=str(exc))
        avisos.append(msg)
        return {**state, "contrato_extraido": None, "extraction_avisos": avisos}

    campos = _parse_campos_structured(resp.content)
    await logger.ainfo("contract_metadata_done", campos_found=list(campos.keys()), tokens=resp.total_tokens)

    if not campos.get("numero_contrato") and not campos.get("objeto"):
        avisos.append("No se pudieron extraer número de contrato ni objeto del texto.")
        return {**state, "contrato_extraido": None, "extraction_avisos": avisos}

    return {
        **state,
        "contrato_extraido": campos,
        "extraction_avisos": avisos,
    }


async def obligations_extraction_node(state: AgentState) -> AgentState:
    """Extract specific obligations from contract text.

    Reads: texto_contrato, contrato_id_str (optional), _db (optional)
    Writes: obligaciones_extraidas, extraction_avisos

    When both _db and contrato_id_str are available, delegates entirely to
    document_service which runs verbatim → LLM → DB persist in one pass.
    Otherwise runs verbatim-first → LLM in stateless mode (no DB persist).
    """
    texto = state.get("texto_contrato") or ""
    if not texto:
        return {**state, "error": "texto_contrato requerido para extracción de obligaciones"}

    contrato_id_str = state.get("contrato_id_str")
    _db = state.get("_db")
    avisos: list[str] = list(state.get("extraction_avisos") or [])

    # ── Fast path: delegate to document service when DB session is available ──
    # document_service already implements verbatim → LLM → persist correctly.
    if _db is not None and contrato_id_str:
        try:
            import uuid as _uuid
            from app.services.document_service import extraer_obligaciones_texto as _extraer_texto

            contrato_id = _uuid.UUID(contrato_id_str)
            saved, svc_avisos = await _extraer_texto(texto, contrato_id, _db)
            all_obligaciones = [
                {"descripcion": o.descripcion, "tipo": o.tipo, "orden": o.orden}
                for o in saved
            ]
            avisos.extend(svc_avisos)
            await logger.ainfo(
                "obligations_extraction_done",
                total=len(all_obligaciones),
                contrato_id_str=contrato_id_str,
                path="service_with_persist",
            )
            return {**state, "obligaciones_extraidas": all_obligaciones, "extraction_avisos": avisos}
        except Exception as exc:
            await logger.awarning("obligations_service_delegation_failed", error=str(exc))
            avisos.append(f"Persistencia falló ({exc}); extrayendo sin guardar.")

    # ── Stateless path: verbatim first, then LLM (no DB persist) ─────────────
    await logger.ainfo(
        "obligations_extraction_start",
        contrato_id_str=contrato_id_str,
        path="stateless",
        model=settings.LLM_EXTRACTION_MODEL or settings.LLM_DEFAULT_MODEL,
    )

    verbatim = _extract_obligaciones_verbatim(texto)
    if verbatim:
        all_obligaciones = [
            {"descripcion": o.descripcion, "tipo": o.tipo, "orden": i}
            for i, o in enumerate(verbatim)
        ]
        await logger.ainfo("obligations_extraction_done", total=len(all_obligaciones), path="verbatim_stateless")
        return {**state, "obligaciones_extraidas": all_obligaciones, "extraction_avisos": avisos}

    extraction_model = settings.LLM_EXTRACTION_MODEL or None
    llm = get_llm(model=extraction_model)
    chunks = _extract_obligation_sections(texto)

    seen_norm: set[str] = set()
    all_obligaciones = []
    llm_errors = 0

    for i, chunk in enumerate(chunks):
        messages = [
            LLMMessage(role="system", content=OBLIGACIONES_SYSTEM),
            LLMMessage(role="user", content=OBLIGACIONES_USER.format(texto_contrato=chunk)),
        ]
        try:
            resp = await llm.complete(messages, temperature=0.0, max_tokens=4096, response_format=ObligacionesLLMList)
        except Exception as exc:
            llm_errors += 1
            await logger.awarning("obligations_chunk_failed", chunk=i, error=str(exc))
            continue

        chunk_obs = _parse_obligaciones_structured(resp.content)
        for ob in chunk_obs:
            norm = ob.descripcion.lower().strip()
            if norm not in seen_norm:
                seen_norm.add(norm)
                all_obligaciones.append({"descripcion": ob.descripcion, "tipo": ob.tipo, "orden": len(all_obligaciones)})

        await logger.ainfo("obligations_chunk_done", chunk=i, found=len(chunk_obs), tokens=resp.total_tokens)

    if llm_errors > 0:
        avisos.append(f"Extracción falló en {llm_errors}/{len(chunks)} fragmentos.")
    if not all_obligaciones and llm_errors == 0:
        avisos.append(
            "No se encontraron obligaciones específicas. "
            "Verifica que el PDF contenga una sección de obligaciones del contratista."
        )

    await logger.ainfo("obligations_extraction_done", total=len(all_obligaciones), contrato_id_str=contrato_id_str)
    return {**state, "obligaciones_extraidas": all_obligaciones, "extraction_avisos": avisos}
