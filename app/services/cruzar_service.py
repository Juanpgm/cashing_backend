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
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.llm import get_llm
from app.agent.nodes.quality_gate import quality_gate_node
from app.agent.prompts.cruzar import (
    CRUZAR_JUSTIFICATION_SYSTEM,
    CRUZAR_JUSTIFICATION_USER,
    CRUZAR_RELEVANCE_BATCH_SYSTEM,
)
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_fuente import DocumentoFuente
from app.schemas.agent import LLMMessage
from app.schemas.cobertura import CoberturaResponse
from app.services import cobertura_service

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


async def _llm_relevance_batch(
    obligation_text: str, candidates: list[dict], llm
) -> list[bool]:
    """Classify ALL candidates for one obligation in a SINGLE LLM call.

    Returns a bool per candidate (same order). Fails closed (all False) on error or
    unparseable output — consistent with the previous "if unsure, not relevant" rule.
    Replaces the former per-candidate _llm_relevance (N calls → 1 per obligation).
    """
    if not candidates:
        return []

    listado = "\n".join(
        f"{i + 1}. {c['content'][:1000]}" for i, c in enumerate(candidates)
    )
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


async def _llm_justification(obligation_text: str, candidate: dict, llm) -> str:
    """Generate a grounded one-sentence justification from the matched evidence."""
    user_content = CRUZAR_JUSTIFICATION_USER.format(
        obligacion=obligation_text[:600],
        documento_fuente=candidate["source"],
        evidencias_texto=candidate["content"][:1500],
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=CRUZAR_JUSTIFICATION_SYSTEM),
                LLMMessage(role="user", content=user_content),
            ],
            temperature=0.0,
            max_tokens=256,
        )
        return resp.content.strip()
    except Exception as exc:
        await logger.awarning("cruzar.justification_llm_error", error=str(exc))
        return f"Evidencia documental referenciada en {candidate['source']}."


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
    # ------------------------------------------------------------------
    await db.execute(delete(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))
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
        candidates = [
            ev for ev in evidence_pool
            if _keyword_score(ob_text, ev["content"]) >= 0.15
        ]

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

            # Step 4d: generate grounded justification
            justificacion = await _llm_justification(ob_text, candidate, llm_justification)

            # Step 4e: create Actividad record
            actividad = Actividad(
                cuenta_cobro_id=cuenta_id,
                obligacion_id=ob.id,
                descripcion=f"Evidencia documental: {candidate['source']}",
                justificacion=justificacion[:1000],
                fecha_realizacion=fecha_realizacion,
            )
            db.add(actividad)
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
                {"id": str(ob.id), "descripcion": ob.descripcion, "tipo": ob.tipo.value}
                for ob in obligaciones
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
