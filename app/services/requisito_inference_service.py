"""Infer a per-cuenta requirements checklist from a contracting-entity document.

Given raw text (pasted) or an uploaded document (PDF/image/docx), use the LLM to
extract the list of documents the contractor must present, normalise the result,
and map obvious items back to the standard catalog so RUT/Cédula/etc. are not
duplicated. The output is a non-persisted preview the user reviews and edits
before applying.
"""

from __future__ import annotations

import re
from decimal import Decimal

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text_match import keyword_score, strip_accents
from app.models.requisito_documento import RequisitoDocumento
from app.schemas.requisito_cuenta import (
    RequisitoCuentaItem,
    RequisitoInferidoLLM,
    RequisitosInferidosLLM,
    RequisitosInferidosPreview,
)

logger = structlog.get_logger("service.requisito_inference")

# Maximum characters of source text sent to the LLM. Requirement lists sit near
# the front of a pliego/estudios previos, so this captures the relevant part
# while keeping the prompt bounded.
_MAX_TEXT_CHARS = 14_000

# Minimum keyword overlap to auto-map an inferred item to a standard requisito.
_MAP_THRESHOLD = Decimal("0.600")

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _slug(value: str) -> str:
    """Normalise a free-form code/label to an UPPER_SNAKE slug without accents."""
    base = strip_accents(value or "").upper()
    slug = _NON_ALNUM.sub("_", base).strip("_")
    return slug[:50]


def _normalizar_keywords(keywords: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for kw in keywords:
        norm = strip_accents(kw or "").lower().strip()
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


async def _map_a_estandar(
    item: RequisitoInferidoLLM,
    codigo: str,
    catalogo: list[RequisitoDocumento],
) -> str | None:
    """Resolve which standard catalog code (if any) an inferred item maps to."""
    codigos = {c.codigo for c in catalogo}
    # 1. Explicit hint from the model.
    if item.mapea_a_estandar and item.mapea_a_estandar.upper() in codigos:
        return item.mapea_a_estandar.upper()
    # 2. The slug itself is a standard code.
    if codigo in codigos:
        return codigo
    # 3. Keyword overlap against each standard's detection keywords.
    haystack = [codigo, item.etiqueta, item.descripcion, *item.keywords_deteccion]
    best_codigo: str | None = None
    best_score = Decimal("0.000")
    for req in catalogo:
        if not req.keywords_deteccion:
            continue
        score = keyword_score(haystack, req.keywords_deteccion)
        if score > best_score:
            best_score = score
            best_codigo = req.codigo
    if best_codigo is not None and best_score >= _MAP_THRESHOLD:
        return best_codigo
    return None


async def inferir_requisitos(db: AsyncSession, texto: str) -> RequisitosInferidosPreview:
    """Infer requirements from raw text. Does NOT persist anything."""
    from app.adapters.llm import get_llm
    from app.agent.prompts.requisitos import REQUISITOS_SYSTEM, construir_user_prompt
    from app.core.config import settings
    from app.schemas.agent import LLMMessage
    from app.services import checklist_service

    avisos: list[str] = []
    texto_limpio = (texto or "").strip()
    if not texto_limpio:
        return RequisitosInferidosPreview(requisitos=[], avisos=["El texto está vacío."])

    catalogo = await checklist_service.listar_catalogo(db)
    catalogo_str = "\n".join(f"- {c.codigo}: {c.etiqueta}" for c in catalogo)
    system = REQUISITOS_SYSTEM.format(catalogo=catalogo_str)

    messages = [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=construir_user_prompt(texto_limpio[:_MAX_TEXT_CHARS])),
    ]

    llm = get_llm(model=settings.LLM_EXTRACTION_MODEL or None)
    try:
        resp = await llm.complete(
            messages,
            temperature=0.0,
            max_tokens=4096,
            response_format=RequisitosInferidosLLM,
        )
    except Exception as exc:
        await logger.awarning("inferir_requisitos_llm_error", error=str(exc)[:200])
        return RequisitosInferidosPreview(
            requisitos=[],
            avisos=["No se pudo procesar el documento con el modelo. Intentá de nuevo o pegá el texto."],
        )

    try:
        parsed = RequisitosInferidosLLM.model_validate_json(resp.content)
    except ValidationError as exc:
        await logger.awarning("inferir_requisitos_parse_failed", error=str(exc)[:200], raw=resp.content[:300])
        return RequisitosInferidosPreview(
            requisitos=[],
            avisos=["El modelo no devolvió una lista de requisitos válida. Revisá el documento."],
        )

    items: list[RequisitoCuentaItem] = []
    vistos: set[str] = set()
    orden = 500
    for raw in parsed.requisitos:
        etiqueta = (raw.etiqueta or "").strip()
        codigo = _slug(raw.codigo or etiqueta)
        if not codigo or not etiqueta:
            continue
        if codigo in vistos:
            continue
        vistos.add(codigo)

        mapea = await _map_a_estandar(raw, codigo, catalogo)
        items.append(
            RequisitoCuentaItem(
                id=None,
                codigo=codigo,
                etiqueta=etiqueta[:200],
                descripcion=(raw.descripcion or "").strip() or None,
                obligatorio=raw.obligatorio,
                solo_primera_cuenta=raw.solo_primera_cuenta,
                tipo_documento_fuente=None,
                keywords_deteccion=_normalizar_keywords(raw.keywords_deteccion),
                orden=orden,
                mapea_a_estandar=mapea,
                origen="inferido",
            )
        )
        orden += 10

    if not items:
        avisos.append("No se detectaron requisitos en el documento.")

    await logger.ainfo("inferir_requisitos_ok", detectados=len(items))
    return RequisitosInferidosPreview(requisitos=items, avisos=avisos)


async def inferir_requisitos_desde_archivo(
    db: AsyncSession,
    filename: str,
    content: bytes,
    content_type: str | None = None,
) -> RequisitosInferidosPreview:
    """Extract text from an uploaded document, then infer requirements from it."""
    from app.services.document_service import extraer_texto_documento

    texto, avisos = await extraer_texto_documento(content, filename)
    if not (texto and texto.strip()):
        return RequisitosInferidosPreview(requisitos=[], avisos=avisos)

    preview = await inferir_requisitos(db, texto)
    # Surface extraction avisos (e.g. OCR notes) ahead of inference avisos.
    preview.avisos = [*avisos, *preview.avisos]
    return preview
