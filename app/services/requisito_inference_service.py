"""Infer a per-cuenta requirements checklist from a contracting-entity document.

Given raw text (pasted) or an uploaded document (PDF/image/docx), use the LLM to
extract the list of documents the contractor must present, normalise the result,
and map obvious items back to the standard catalog so RUT/Cédula/etc. are not
duplicated. The output is a non-persisted preview the user reviews and edits
before applying.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.core.exceptions import ValidationError as DomainValidationError
from app.core.text_match import keyword_score, normalize, strip_accents
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.plantilla_organismo import PlantillaOrganismo
from app.models.requisito_documento import RequisitoDocumento
from app.schemas.plantilla_organismo import EstructuraPlantillaLLM
from app.schemas.requisito_cuenta import (
    RequisitoCuentaItem,
    RequisitoEstructuradoItem,
    RequisitoInferidoLLM,
    RequisitosEstructuradosLLM,
    RequisitosEstructuradosPreview,
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


# ── Structured requisito extraction (billing-resilience-templates, slice #7) ─
#
# Extends `inferir_requisitos`: reuses the exact same catalog-mapping pipeline
# (`_slug`, `_map_a_estandar`, `_normalizar_keywords`) but requests + returns a
# RICHER structured output — `categoria` and `permite_autogen` on top of the
# existing `solo_primera_cuenta` flag — instead of a flat list. Non-persisted
# preview, same as `inferir_requisitos`: `categoria`/`permite_autogen` are NOT
# columns on `RequisitoCuenta` (no migration in this slice); applying a
# reviewed subset still goes through the existing `POST /definir`.


async def inferir_requisitos_estructurados(db: AsyncSession, texto: str) -> RequisitosEstructuradosPreview:
    """Structured variant of `inferir_requisitos`. Does NOT persist anything."""
    from app.adapters.llm import get_llm
    from app.agent.prompts.requisitos import (
        REQUISITOS_ESTRUCTURADOS_SYSTEM,
        construir_user_prompt_estructurado,
    )
    from app.core.config import settings
    from app.schemas.agent import LLMMessage
    from app.services import checklist_service

    avisos: list[str] = []
    texto_limpio = (texto or "").strip()
    if not texto_limpio:
        return RequisitosEstructuradosPreview(requisitos=[], avisos=["El texto está vacío."])

    catalogo = await checklist_service.listar_catalogo(db)
    catalogo_str = "\n".join(f"- {c.codigo}: {c.etiqueta}" for c in catalogo)
    system = REQUISITOS_ESTRUCTURADOS_SYSTEM.format(catalogo=catalogo_str)

    messages = [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=construir_user_prompt_estructurado(texto_limpio[:_MAX_TEXT_CHARS])),
    ]

    llm = get_llm(model=settings.LLM_EXTRACTION_MODEL or None)
    try:
        resp = await llm.complete(
            messages,
            temperature=0.0,
            max_tokens=4096,
            response_format=RequisitosEstructuradosLLM,
        )
    except Exception as exc:
        await logger.awarning("inferir_requisitos_estructurados_llm_error", error=str(exc)[:200])
        return RequisitosEstructuradosPreview(
            requisitos=[],
            avisos=["No se pudo procesar el documento con el modelo. Intentá de nuevo o pegá el texto."],
        )

    try:
        parsed = RequisitosEstructuradosLLM.model_validate_json(resp.content)
    except ValidationError as exc:
        await logger.awarning(
            "inferir_requisitos_estructurados_parse_failed", error=str(exc)[:200], raw=resp.content[:300]
        )
        return RequisitosEstructuradosPreview(
            requisitos=[],
            avisos=["El modelo no devolvió una lista de requisitos válida. Revisá el documento."],
        )

    items: list[RequisitoEstructuradoItem] = []
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

        # `_map_a_estandar` only reads `mapea_a_estandar`/`etiqueta`/`descripcion`/
        # `keywords_deteccion` — `RequisitoEstructuradoLLM` carries all of them.
        mapea = await _map_a_estandar(raw, codigo, catalogo)
        items.append(
            RequisitoEstructuradoItem(
                id=None,
                codigo=codigo,
                etiqueta=etiqueta[:200],
                categoria=(raw.categoria or "").strip().lower(),
                descripcion=(raw.descripcion or "").strip() or None,
                obligatorio=raw.obligatorio,
                solo_primera_cuenta=raw.solo_primera_cuenta,
                permite_autogen=raw.permite_autogen,
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

    await logger.ainfo("inferir_requisitos_estructurados_ok", detectados=len(items))
    return RequisitosEstructuradosPreview(requisitos=items, avisos=avisos)


# ── Template structure extraction (billing-resilience-templates, slice #5) ───
#
# Ingests an institutional informe template (DOCX/PDF) per organism, extracts
# its STRUCTURE (column layout, section list, literal anexo references) via
# the LLM, and persists it keyed to the organism (`PlantillaOrganismo`,
# migration 027). Degrades gracefully: any extraction failure returns `None`
# instead of raising — this is never a hard error, it just means
# `adaptive-informe-generation` (slice #6) and the packager fall back to their
# default flat layout for that organism.
#
# NOTE on `estructura_json` shape: design D5's shorthand lists
# `anexo_refs: bool`. This implementation stores `anexo_refs: list[str]` — the
# literal anexo-reference strings themselves — because the spec's explicit
# acceptance criterion is "Anexo reference preserved verbatim" (a boolean flag
# cannot satisfy that). `estructura_json` is an unconstrained JSON blob (no DB
# schema enforces its shape), so this is an additive superset of the design's
# intent, not a contradiction: `bool(anexo_refs)` recovers the flag design
# describes, if a consumer ever needs it.

_ESTRUCTURA_PLANTILLA_SYSTEM = """\
Eres un experto en informes institucionales de supervisión y de actividades de \
contratistas del Estado colombiano. Se te entrega una plantilla oficial (informe \
de actividades o de supervisión) emitida por una entidad contratante.

Extrae ÚNICAMENTE la ESTRUCTURA del documento, nunca contenido específico de un \
periodo o contratista:
- `columnas`: encabezados de la tabla principal de seguimiento de obligaciones, \
en el orden en que aparecen (ej: ["obligación", "avance del periodo"] o \
["obligación", "avance del periodo", "evidencia"]).
- `secciones`: títulos de las secciones del informe, en el orden en que aparecen \
(ej: ["INFORME JURÍDICO", "INFORME CONTABLE", "INFORME DE APORTES", "INFORME TÉCNICO"]).
- `anexo_refs`: toda referencia literal a anexos o carpetas de evidencia tal como \
aparece en el texto (ej: "Ver Anexo: Carpeta /5. EVIDENCIAS/A1"), preservada \
EXACTAMENTE como está escrita. Si no hay ninguna, deja la lista vacía.
- `notas`: cualquier observación breve relevante para replicar el formato.

Responde ÚNICAMENTE el JSON del esquema, sin texto adicional.
"""

_ESTRUCTURA_PLANTILLA_VISION_USER = (
    "Analiza esta plantilla institucional (imagen o PDF escaneado) y extrae su estructura según el esquema indicado."
)


def _construir_user_prompt_estructura(texto: str) -> str:
    return f"PLANTILLA INSTITUCIONAL (extracto):\n---\n{texto}\n---\nExtrae la estructura de esta plantilla."


async def _extraer_estructura_via_texto(texto: str) -> EstructuraPlantillaLLM | None:
    """LLM structured-output extraction over already-extracted plain text."""
    from app.adapters.llm import get_llm
    from app.core.config import settings
    from app.schemas.agent import LLMMessage

    messages = [
        LLMMessage(role="system", content=_ESTRUCTURA_PLANTILLA_SYSTEM),
        LLMMessage(role="user", content=_construir_user_prompt_estructura(texto)),
    ]
    llm = get_llm(model=settings.LLM_EXTRACTION_MODEL or None)
    try:
        resp = await llm.complete(
            messages,
            temperature=0.0,
            max_tokens=2048,
            response_format=EstructuraPlantillaLLM,
        )
    except Exception as exc:
        await logger.awarning("inferir_estructura_plantilla_llm_error", error=str(exc)[:200])
        return None

    try:
        return EstructuraPlantillaLLM.model_validate_json(resp.content)
    except ValidationError as exc:
        await logger.awarning("inferir_estructura_plantilla_parse_failed", error=str(exc)[:200], raw=resp.content[:300])
        return None


async def _extraer_estructura_via_vision(content: bytes, filename: str) -> EstructuraPlantillaLLM | None:
    """Vision-model fallback, retried through the same resilient chain CONTRATO
    extraction uses (`document_service.vision_model_chain`) before giving up —
    for scanned/degraded templates the text ladder cannot read."""
    from app.adapters.llm import get_llm
    from app.agent.tools.multimodal_parser import (
        build_multimodal_content_parts,
        guess_mime_type,
        is_multimodal_supported,
    )
    from app.core.config import settings
    from app.schemas.agent import LLMMessage
    from app.services import document_service

    mime = guess_mime_type(filename)
    if not is_multimodal_supported(mime):
        return None

    chain = document_service.vision_model_chain()
    for model in chain:
        try:
            parts = build_multimodal_content_parts(
                content,
                mime,
                model,
                max_pdf_pages=settings.MULTIMODAL_MAX_PDF_PAGES,
                dpi=settings.MULTIMODAL_RASTER_DPI,
            )
            messages = [
                LLMMessage(role="system", content=_ESTRUCTURA_PLANTILLA_SYSTEM),
                LLMMessage(
                    role="user",
                    content=[{"type": "text", "text": _ESTRUCTURA_PLANTILLA_VISION_USER}, *parts],
                ),
            ]
            # fallback=False: this function manages its own vision-aware chain instead
            # of the generic text-only fallback (same rationale as
            # `document_service._extraer_contrato_multimodal`).
            resp = await get_llm(model=model).complete(
                messages,
                temperature=0.0,
                max_tokens=2048,
                response_format=EstructuraPlantillaLLM,
                fallback=False,
            )
        except Exception as exc:
            await logger.awarning("inferir_estructura_plantilla_vision_model_failed", model=model, error=str(exc)[:200])
            continue

        try:
            return EstructuraPlantillaLLM.model_validate_json(resp.content)
        except ValidationError as exc:
            await logger.awarning("inferir_estructura_plantilla_vision_parse_failed", model=model, error=str(exc)[:200])
            continue

    return None


async def inferir_estructura_plantilla(filename: str, content: bytes) -> EstructuraPlantillaLLM | None:
    """Best-effort structure extraction from an institutional informe template.

    Text-first: extracts text via the standard ladder (`document_service.
    extraer_texto_documento`, which already includes local OCR), then LLM-
    completes a structured extraction over it. If no usable text was
    recovered — or the text-path extraction failed — retries through the
    resilient vision-model fallback chain before giving up. Returns `None` on
    any failure; this is a graceful-degradation path, never an error (spec:
    "No new error code; extraction failure is a graceful-degradation path").
    """
    from app.services import document_service

    texto, _avisos = await document_service.extraer_texto_documento(content, filename)
    if texto and texto.strip():
        estructura = await _extraer_estructura_via_texto(texto[:_MAX_TEXT_CHARS])
        if estructura is not None:
            return estructura

    return await _extraer_estructura_via_vision(content, filename)


# Only these DocumentoFuente types are valid ingestion targets for a per-organism
# template structure — billing-resilience-templates, slice #7, task 7.5b (carry-over
# from slice #5 verify-report WARNING + SUGGESTION b): a CEDULA/RUT/etc. must never
# be storable as a plantilla outside this documented domain.
_TIPOS_PLANTILLA_VALIDOS = {TipoDocumentoFuente.INFORME_ACTIVIDADES, TipoDocumentoFuente.INFORME_SUPERVISION}


async def _get_contrato_con_ownership(db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID) -> Contrato:
    contrato = await db.get(Contrato, contrato_id)
    if contrato is None:
        raise NotFoundError("Contrato", str(contrato_id))
    if contrato.usuario_id != usuario_id:
        raise ForbiddenError()
    return contrato


async def ingerir_plantilla_organismo(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    documento_fuente_id: uuid.UUID,
) -> tuple[PlantillaOrganismo | None, list[str]]:
    """Extract and persist a per-organism template structure from an
    already-uploaded document (`DocumentoFuente`).

    Graceful degradation (spec): if extraction fails, returns ``(None,
    avisos)`` — nothing is persisted and nothing is raised. Ingestion of the
    underlying contrato/documento record already happened separately (via the
    normal upload flow) and is never blocked by this call.

    Re-ingesting for the same organism/tipo_documento UPDATES the existing row
    (unique on `usuario_id, entidad_normalizada, tipo_documento`) rather than
    accumulating duplicates.
    """
    from app.adapters.storage import get_storage
    from app.core.config import settings

    contrato = await _get_contrato_con_ownership(db, usuario_id, contrato_id)
    if not contrato.entidad or not contrato.entidad.strip():
        raise DomainValidationError(
            "El contrato no tiene una entidad contratante definida; no se puede asociar la plantilla a un organismo."
        )

    doc = await db.get(DocumentoFuente, documento_fuente_id)
    if doc is None:
        raise NotFoundError("Documento", str(documento_fuente_id))
    if doc.usuario_id != usuario_id:
        raise ForbiddenError()
    if doc.tipo not in _TIPOS_PLANTILLA_VALIDOS:
        raise DomainValidationError(
            "El documento indicado no es un tipo de plantilla institucional válido "
            "(se esperaba informe_actividades o informe_supervision); no se puede "
            "ingerir como plantilla de organismo."
        )

    storage = get_storage(settings.S3_BUCKET_DOCUMENTOS)
    content = await storage.download(doc.storage_key)

    estructura = await inferir_estructura_plantilla(doc.nombre, content)
    if estructura is None:
        await logger.awarning(
            "ingerir_plantilla_organismo_degradado", contrato_id=str(contrato_id), documento_id=str(documento_fuente_id)
        )
        return None, [
            "No se pudo extraer la estructura de la plantilla institucional. "
            "Se usará el formato de informe por defecto para este organismo."
        ]

    entidad_normalizada = normalize(contrato.entidad)
    tipo_documento = doc.tipo.value if doc.tipo else "informe_actividades"
    formato = "docx" if doc.nombre.lower().endswith(".docx") else "pdf"

    result = await db.execute(
        select(PlantillaOrganismo).where(
            PlantillaOrganismo.usuario_id == usuario_id,
            PlantillaOrganismo.entidad_normalizada == entidad_normalizada,
            PlantillaOrganismo.tipo_documento == tipo_documento,
        )
    )
    plantilla = result.scalar_one_or_none()
    estructura_dict = estructura.model_dump()
    if plantilla is not None:
        plantilla.entidad = contrato.entidad
        plantilla.formato = formato
        plantilla.estructura_json = estructura_dict
        plantilla.fuente_documento_id = documento_fuente_id
    else:
        plantilla = PlantillaOrganismo(
            usuario_id=usuario_id,
            entidad=contrato.entidad,
            entidad_normalizada=entidad_normalizada,
            tipo_documento=tipo_documento,
            formato=formato,
            estructura_json=estructura_dict,
            fuente_documento_id=documento_fuente_id,
        )
        db.add(plantilla)

    await db.flush()
    await db.refresh(plantilla)
    await logger.ainfo("ingerir_plantilla_organismo_ok", contrato_id=str(contrato_id), entidad=entidad_normalizada)
    return plantilla, []


async def obtener_plantilla_organismo_por_contrato(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato: Contrato,
    tipo_documento: str = "informe_actividades",
) -> PlantillaOrganismo | None:
    """Core lookup given an ALREADY loaded+ownership-validated `Contrato`.

    Public (not underscore-prefixed) so a caller that already holds the
    `Contrato` object — e.g. `informe_service._resolver_estructura_organismo`,
    which loads+validates it via its own `_load_context` — can skip the
    redundant ownership round-trip `obtener_plantilla_organismo` performs for
    callers who only have IDs (billing-resilience-templates, slice #7, task
    7.5c; carry-over from slice #5 verify-report SUGGESTION a). Mirrors the
    `document_service.vision_model_chain()` precedent (slice #5, task 5.12) of
    promoting an internal helper to a public cross-module seam instead of
    duplicating logic.
    """
    if not contrato.entidad or not contrato.entidad.strip():
        return None

    entidad_normalizada = normalize(contrato.entidad)
    result = await db.execute(
        select(PlantillaOrganismo).where(
            PlantillaOrganismo.usuario_id == usuario_id,
            PlantillaOrganismo.entidad_normalizada == entidad_normalizada,
            PlantillaOrganismo.tipo_documento == tipo_documento,
        )
    )
    return result.scalar_one_or_none()


async def obtener_plantilla_organismo(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    tipo_documento: str = "informe_actividades",
) -> PlantillaOrganismo | None:
    """Read-only lookup of the persisted template structure for a contract's
    organism (normalized `Contrato.entidad` match). Returns `None` when no
    template has ever been ingested for that organism/tipo_documento."""
    contrato = await _get_contrato_con_ownership(db, usuario_id, contrato_id)
    return await obtener_plantilla_organismo_por_contrato(db, usuario_id, contrato, tipo_documento)
