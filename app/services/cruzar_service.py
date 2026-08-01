"""Cruzar service — document-to-obligation matching that populates Actividad records.

Orchestrates: load docs from contrato → keyword + LLM match against obligations →
create Actividad records → run quality gate (non-blocking) → return CoberturaResponse.

This is a "refresh from docs" operation: existing Actividades for the cuenta_cobro
are deleted before re-running so the result is always consistent with the current
set of uploaded documents.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.llm import get_llm
from app.agent.nodes.quality_gate import quality_gate_node
from app.agent.prompts.actividad_generation import is_near_identical
from app.agent.prompts.cruzar import (
    CRUZAR_ACTIVIDAD_SYSTEM,
    CRUZAR_ACTIVIDAD_USER,
    CRUZAR_JUSTIFICATION_SYSTEM,
    CRUZAR_JUSTIFICATION_USER,
    CRUZAR_RELEVANCE_BATCH_SYSTEM,
)
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_fuente import DocumentoFuente
from app.models.evidencia import Evidencia
from app.schemas.agent import LLMMessage
from app.schemas.cobertura import CoberturaResponse
from app.services import checklist_service, cobertura_service

logger = structlog.get_logger("service.cruzar")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _keyword_score(obligation_text: str, evidence_text: str) -> float:
    """Compute token overlap between obligation description and evidence content.

    Score = |words_in_obligation ∩ words_in_evidence| / |words_in_obligation|
    Tokens must be ≥4 characters to filter stop-words.
    """
    if not obligation_text or not evidence_text:
        return 0.0

    ob_words = set(re.findall(r"[a-záéíóúñüA-ZÁÉÍÓÚÑÜ]{4,}", obligation_text.lower()))
    ev_words = set(re.findall(r"[a-záéíóúñüA-ZÁÉÍÓÚÑÜ]{4,}", evidence_text.lower()))

    if not ob_words:
        return 0.0
    return len(ob_words & ev_words) / len(ob_words)


_RELEVANCE_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)


async def _llm_relevance_batch(obligation_text: str, candidates: list[dict], llm) -> list[bool]:
    """Classify ALL candidates for one obligation in a SINGLE LLM call.

    Returns a bool per candidate (same order). Fails closed (all False) on error or
    unparseable output — consistent with the previous "if unsure, not relevant" rule.
    Replaces the former per-candidate _llm_relevance (N calls → 1 per obligation).
    """
    if not candidates:
        return []

    listado = "\n".join(f"{i + 1}. {c['content'][:1000]}" for i, c in enumerate(candidates))
    user_content = (
        f"Obligación: {obligation_text[:500]}\n\n"
        f"Evidencias:\n{listado}\n\n"
        "¿Cuáles evidencias son relevantes? Responde solo el array JSON de números."
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=CRUZAR_RELEVANCE_BATCH_SYSTEM),
                LLMMessage(role="user", content=user_content),
            ],
            temperature=0.0,
            max_tokens=64,
        )
        match = _RELEVANCE_JSON_RE.search(resp.content)
        if not match:
            return [False] * len(candidates)
        nums = json.loads(match.group(0))
        relevant_idx = {int(n) for n in nums if isinstance(n, (int, float))}
        return [(i + 1) in relevant_idx for i in range(len(candidates))]
    except Exception as exc:
        await logger.awarning("cruzar.relevance_llm_error", error=str(exc))
        return [False] * len(candidates)


def _contexto_usuario_bloque(contexto_usuario: str) -> str:
    """Optional user-monthly-summary block for the cruzar writer prompts.

    Writing guidance only — the anti-hallucination contract stays intact: quotes
    must still come EXCLUSIVELY from the document fragments.
    """
    if not contexto_usuario:
        return ""
    return (
        "\nContexto del contratista sobre su mes (SOLO como orientación de redacción; "
        "NUNCA cites de aquí — las citas salen exclusivamente de los fragmentos):\n"
        f"{contexto_usuario[:1000]}\n"
    )


# Bounds for the contract-level context block (clasificacion-documentos-secop, D4).
_CONTEXTO_CONTRATO_MAX_DOCS = 5
_CONTEXTO_CONTRATO_SNIPPET_MAX = 400
_CONTEXTO_CONTRATO_MAX_TOTAL = 2500


def _contexto_contrato_bloque(contexto_docs: list[dict[str, Any]]) -> str:
    """Optional contract-level context block for the cruzar writer prompts.

    Built from `checklist_service.listar_documentos_contexto` items (OTROS/
    unmapped, unlinked contract docs). Bounded: <=5 docs, <=400-char snippet
    each, <=2500 chars total. Docs without extracted text contribute a
    metadata-only line — this block NEVER triggers a download. Zero docs →
    empty string, so existing prompts stay byte-identical. Same
    anti-hallucination contract as `_contexto_usuario_bloque`: guidance only,
    quotes still come exclusively from the document fragments.
    """
    if not contexto_docs:
        return ""
    bloque = (
        "\nDocumentos de contexto del contrato (SOLO como orientación; "
        "NUNCA cites de aquí — las citas salen exclusivamente de los fragmentos):\n"
    )
    for doc in contexto_docs[:_CONTEXTO_CONTRATO_MAX_DOCS]:
        snippet = (doc.get("snippet") or "")[:_CONTEXTO_CONTRATO_SNIPPET_MAX]
        linea = f"- {doc['nombre']}: {snippet}\n" if snippet else f"- {doc['nombre']} (sin texto extraído)\n"
        if len(bloque) + len(linea) > _CONTEXTO_CONTRATO_MAX_TOTAL:
            break
        bloque += linea
    return bloque


async def _llm_justification(
    obligation_text: str, candidate: dict, llm, contexto_usuario: str = "", contexto_contrato: str = ""
) -> str:
    """Generate a grounded one-sentence justification from the matched evidence."""
    user_content = (
        CRUZAR_JUSTIFICATION_USER.format(
            obligacion=obligation_text[:600],
            documento_fuente=candidate["source"],
            evidencias_texto=candidate["content"][:1500],
        )
        + _contexto_usuario_bloque(contexto_usuario)
        + contexto_contrato
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=CRUZAR_JUSTIFICATION_SYSTEM),
                LLMMessage(role="user", content=user_content),
            ],
            temperature=0.0,
            # gemini-2.5-flash: los tokens de thinking cuentan dentro de max_tokens;
            # 256 trunca la salida a mitad de frase.
            max_tokens=2048,
        )
        return resp.content.strip()
    except Exception as exc:
        await logger.awarning("cruzar.justification_llm_error", error=str(exc))
        return f"Evidencia documental referenciada en {candidate['source']}."


async def _llm_actividad(
    obligation_text: str, candidate: dict, llm, contexto_usuario: str = "", contexto_contrato: str = ""
) -> str:
    """Generate a grounded one-sentence ACTIVIDAD (what was done) from the matched
    document — distinct from `_llm_justification` (why it satisfies the obligación).

    Degraded fallback on LLM error: a deterministic sentence naming the source
    document — NEVER the raw "Evidencia documental: {source}" placeholder text,
    and never the obligación text.
    """
    user_content = (
        CRUZAR_ACTIVIDAD_USER.format(
            obligacion=obligation_text[:600],
            documento_fuente=candidate["source"],
            evidencias_texto=candidate["content"][:1500],
        )
        + _contexto_usuario_bloque(contexto_usuario)
        + contexto_contrato
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=CRUZAR_ACTIVIDAD_SYSTEM),
                LLMMessage(role="user", content=user_content),
            ],
            temperature=0.0,
            # gemini-2.5-flash: los tokens de thinking cuentan dentro de max_tokens;
            # 256 trunca la salida a mitad de frase.
            max_tokens=2048,
        )
        actividad = resp.content.strip()
        return actividad or f"Elaboración y entrega de {candidate['source']}."
    except Exception as exc:
        await logger.awarning("cruzar.actividad_llm_error", error=str(exc))
        return f"Elaboración y entrega de {candidate['source']}."


# ---------------------------------------------------------------------------
# Public service function
# ---------------------------------------------------------------------------


async def cruzar_documentos(
    db: AsyncSession,
    usuario_id: UUID,
    cuenta_id: UUID,
) -> CoberturaResponse:
    """Match uploaded documents to contract obligations and persist Actividad records.

    Steps:
    1. Load CuentaCobro (with contrato → obligaciones)
    2. Load DocumentoFuente records that have extracted text
    3. Delete existing Actividades for this cuenta (refresh semantics)
    4. For each Obligacion: keyword filter → LLM binary relevance → create Actividad
    5. Run quality gate (non-blocking — fail open)
    6. Return CoberturaResponse via cobertura_service
    """
    # ------------------------------------------------------------------
    # 1. Load CuentaCobro and verify ownership
    # ------------------------------------------------------------------
    result = await db.execute(
        select(CuentaCobro)
        .options(
            selectinload(CuentaCobro.contrato).selectinload(Contrato.obligaciones),
            selectinload(CuentaCobro.actividades),
        )
        .where(CuentaCobro.id == cuenta_id, CuentaCobro.deleted_at.is_(None))
    )
    cuenta = result.scalar_one_or_none()
    if cuenta is None:
        raise NotFoundError("CuentaCobro", str(cuenta_id))
    if cuenta.contrato.usuario_id != usuario_id:
        raise ForbiddenError()

    contrato = cuenta.contrato
    obligaciones = contrato.obligaciones

    await logger.ainfo(
        "cruzar.start",
        cuenta_id=str(cuenta_id),
        contrato_id=str(contrato.id),
        n_obligaciones=len(obligaciones),
    )

    # ------------------------------------------------------------------
    # 2. Load DocumentoFuente records with extracted text
    # ------------------------------------------------------------------
    docs_result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.contrato_id == contrato.id,
            DocumentoFuente.texto_extraido.is_not(None),
            DocumentoFuente.texto_extraido != "",
        )
    )
    documentos = list(docs_result.scalars().all())

    if not documentos:
        await logger.awarning(
            "cruzar.no_docs_with_text",
            contrato_id=str(contrato.id),
            message="No DocumentoFuente with texto_extraido found — returning current cobertura",
        )
        return await cobertura_service.calcular_cobertura(db, usuario_id, cuenta_id)

    # ------------------------------------------------------------------
    # 3. Delete existing Actividades for this cuenta (refresh from docs)
    # Deleting first ensures a clean, idempotent re-run — any previous
    # AI-generated activities are replaced with the current document set.
    # Rows that carry uploaded Evidencia are kept out of the delete: they're
    # the placeholder stubs `evidencia_service.subir_evidencias_cuenta` creates
    # when evidence is uploaded ahead of this step, and Evidencia.actividad_id
    # has no ON DELETE clause — deleting them would orphan the evidence (SQLite)
    # or raise a FK violation (Postgres). They're reused/filled below instead.
    # ------------------------------------------------------------------
    stubs_result = await db.execute(
        select(Actividad)
        .join(Evidencia, Evidencia.actividad_id == Actividad.id)
        .where(Actividad.cuenta_cobro_id == cuenta_id)
        .distinct()
    )
    stubs_con_evidencia: dict[UUID | None, Actividad] = {act.obligacion_id: act for act in stubs_result.scalars()}
    delete_stmt = delete(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id)
    if stubs_con_evidencia:
        delete_stmt = delete_stmt.where(Actividad.id.not_in([act.id for act in stubs_con_evidencia.values()]))
    await db.execute(delete_stmt)
    await logger.ainfo("cruzar.deleted_existing_actividades", cuenta_id=str(cuenta_id))

    # ------------------------------------------------------------------
    # 4. Build evidence pool and match per obligation
    # ------------------------------------------------------------------
    evidence_pool = [
        {
            "content": (doc.texto_extraido or "")[:2000],
            "source": doc.nombre,
            "tipo": doc.tipo.value,
        }
        for doc in documentos
    ]

    # Contract-level context docs (OTROS/unmapped, unlinked) for the writer
    # prompts — read-only and fail-open: a context failure never blocks cruzar.
    try:
        contexto_docs = await checklist_service.listar_documentos_contexto(db, cuenta)
    except Exception as exc:
        await logger.awarning("cruzar.contexto_contrato_error", error=str(exc))
        contexto_docs = []
    contexto_contrato = _contexto_contrato_bloque(contexto_docs)

    # LLM clients (reused across all obligations for connection efficiency)
    llm_relevance = get_llm(model="groq/llama-3.1-8b-instant")
    llm_justification = get_llm(model="gemini/gemini-2.5-flash")

    # Compute last day of the billing month for fecha_realizacion
    last_day = calendar.monthrange(cuenta.anio, cuenta.mes)[1]
    fecha_realizacion = date(cuenta.anio, cuenta.mes, last_day)

    actividades_creadas = 0

    for ob in obligaciones:
        ob_text = ob.descripcion or ""

        # Step 4a: keyword filter (threshold ≥ 0.15)
        candidates = [ev for ev in evidence_pool if _keyword_score(ob_text, ev["content"]) >= 0.15]

        if not candidates:
            await logger.ainfo(
                "cruzar.obligacion_skipped_no_candidates",
                obligacion_id=str(ob.id),
                descripcion=ob_text[:80],
            )
            continue

        # Step 4b: sort by keyword score descending, keep top-5
        candidates = sorted(
            candidates,
            key=lambda e: _keyword_score(ob_text, e["content"]),
            reverse=True,
        )[:5]

        # Step 4c: LLM relevance — ONE batched call per obligation, not one per candidate
        flags = await _llm_relevance_batch(ob_text, candidates, llm_relevance)
        for candidate, is_relevant in zip(candidates, flags):
            if not is_relevant:
                await logger.ainfo(
                    "cruzar.candidate_not_relevant",
                    obligacion_id=str(ob.id),
                    source=candidate["source"],
                )
                continue

            # Step 4d: generate grounded actividad (what was done) + justificación (why it counts)
            contexto_usuario = (cuenta.contexto_usuario or "").strip()
            actividad_texto = await _llm_actividad(
                ob_text, candidate, llm_justification, contexto_usuario, contexto_contrato
            )
            justificacion = await _llm_justification(
                ob_text, candidate, llm_justification, contexto_usuario, contexto_contrato
            )

            if is_near_identical(actividad_texto, justificacion):
                # Both texts collapsed to the same content — fall back to the
                # deterministic actividad text so the two fields stay distinct.
                actividad_texto = f"Elaboración y entrega de {candidate['source']}."

            # Step 4e: create the Actividad record — reusing this obligación's
            # evidence-carrying stub (first match only) instead of inserting a
            # fresh row, so the uploaded Evidencia stays linked to the row that
            # now holds the real, generated text.
            stub = stubs_con_evidencia.pop(ob.id, None)
            if stub is not None:
                stub.descripcion = actividad_texto[:1000]
                stub.justificacion = justificacion[:1000]
                stub.fecha_realizacion = fecha_realizacion
            else:
                db.add(
                    Actividad(
                        cuenta_cobro_id=cuenta_id,
                        obligacion_id=ob.id,
                        descripcion=actividad_texto[:1000],
                        justificacion=justificacion[:1000],
                        fecha_realizacion=fecha_realizacion,
                    )
                )
            actividades_creadas += 1

            await logger.ainfo(
                "cruzar.actividad_creada",
                obligacion_id=str(ob.id),
                source=candidate["source"],
                justificacion_preview=justificacion[:80],
            )

    # ------------------------------------------------------------------
    # 5. Flush all new Actividades in a single round-trip
    # ------------------------------------------------------------------
    await db.flush()

    await logger.ainfo(
        "cruzar.done",
        cuenta_id=str(cuenta_id),
        actividades_creadas=actividades_creadas,
        n_obligaciones=len(obligaciones),
    )

    # ------------------------------------------------------------------
    # 6. Quality gate — non-blocking (fail open)
    # Builds a minimal state dict compatible with quality_gate_node.
    # ------------------------------------------------------------------
    try:
        gate_state = {
            "obligaciones_extraidas": [
                {"id": str(ob.id), "descripcion": ob.descripcion, "tipo": ob.tipo.value} for ob in obligaciones
            ],
            "contrato_extraido": {
                "objeto": contrato.objeto or "",
                "numero_contrato": contrato.numero_contrato or "",
            },
        }
        gate_result = await quality_gate_node(gate_state)
        await logger.ainfo(
            "cruzar.quality_gate",
            passed=gate_result.get("quality_gate_passed"),
            issues=gate_result.get("quality_issues", []),
        )
    except Exception as exc:
        await logger.awarning("cruzar.quality_gate_error", error=str(exc))

    # ------------------------------------------------------------------
    # 7. Return updated cobertura
    # ------------------------------------------------------------------
    return await cobertura_service.calcular_cobertura(db, usuario_id, cuenta_id)
