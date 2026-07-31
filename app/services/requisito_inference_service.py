"""Infer a per-cuenta requirements checklist from a contracting-entity document.

Given raw text (pasted) or an uploaded document (PDF/image/docx), use the LLM to
extract the list of documents the contractor must present, normalise the result,
and map obvious items back to the standard catalog so RUT/Cédula/etc. are not
duplicated. The output is a non-persisted preview the user reviews and edits
before applying.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid
from decimal import Decimal
from pathlib import Path

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import DomainError, ForbiddenError, NotFoundError
from app.core.exceptions import ValidationError as DomainValidationError
from app.core.text_match import keyword_score, normalize, strip_accents
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.plantilla_organismo import PlantillaOrganismo
from app.models.requisito_documento import RequisitoDocumento
from app.schemas.plantilla_organismo import (
    CamposPlantillaLLM,
    ClasificacionArchivoLLM,
    EstructuraPlantillaLLM,
    MiembroClasificadoLLM,
    MiembroConfirmado,
    MiembroIngestaResultado,
    MiembroPropuesto,
)
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


# ── Clonable-template campo detection (docx clone, phase 2) ──────────────────
#
# Given a FILLED example DOCX (an addressed skeleton from `docx_clone_service.
# extraer_esqueleto`), the LLM identifies every VARIABLE value and maps it to
# the closed canonical vocabulary below. `docx_clone_service.validar_campos`
# then deterministically drops any campo whose address/valor_ejemplo doesn't
# actually match the document — the LLM proposes, the validator disposes.

_CAMPOS_PLANTILLA_SYSTEM = """\
You are an expert in Colombian government contractor billing documents (cuentas \
de cobro, informes de actividades/supervisión). You receive the SKELETON of a \
FILLED example report from a contracting entity: one line per document node, \
formatted as "direccion: text" (P{i} = body paragraph, T{t}.R{r}.C{c} = table \
cell, nested tables chain addresses, all 0-based in body order). Spreadsheet \
(xlsx) skeletons address lines as "{sheet_name}!{cell}" instead (e.g. \
"Documento Soporte!C7") and ONLY modo "cell" applies to them.

Identify every VARIABLE value — data that changes per contract, per installment \
(cuota), or per period — and return it as a campo object. Static form text \
(labels, headings, legal boilerplate) is NOT a campo.

For each campo:
- "direccion": the address of the line where the value appears, copied exactly.
- "etiqueta": the label near the value (e.g. "Contrato No.", "Valor Cuota a \
cancelar", "Cuota Número", "Nombre completo del contratista:").
- "valor_ejemplo": the example value copied VERBATIM from the skeleton line — \
never reformatted, never paraphrased.
- "campo": one key from the closed vocabulary below. Never invent keys.
- "modo":
  - "cell": the node's text is ENTIRELY the value.
  - "substring": the value is embedded in surrounding text ("Label: value") — \
valor_ejemplo is only the value part.
  - "checkbox": an X/☒ marker choosing between options such as INFORME PARCIAL \
vs INFORME FINAL. "direccion" is the CURRENTLY MARKED option (valor_ejemplo = \
the marker text), "direccion_alternativa" is the other option's address and \
"valor_alternativo" the marker to write there. Use campo "computed.tipo_informe".
  - "justificacion": the free-text narrative cell of an obligation/activity \
row. Use campo "justificacion.{orden}", numbering rows top-to-bottom starting \
at 1; "etiqueta" holds the literal row label (e.g. "A)" or "ACTIVIDAD 1").

Closed campo vocabulary:
- contrato.numero_contrato, contrato.objeto, contrato.entidad, \
contrato.dependencia, contrato.valor_total, contrato.valor_mensual, \
contrato.fecha_inicio, contrato.fecha_fin, contrato.supervisor_nombre, \
contrato.cargo_supervisor
- contratista.nombre, contratista.cedula
- cuota.valor, cuota.numero, cuota.mes, cuota.anio, cuota.periodo, cuota.fecha, \
cuota.consecutivo_ds (Documento Soporte consecutive number, user-supplied per cuota)
- computed.acumulado, computed.saldo, computed.valor_total_con_adiciones, \
computed.valor_letras, computed.cuota_ordinal_letras, computed.tipo_informe
- justificacion.{orden}

Respond ONLY with JSON matching the schema, no extra text.
"""


def _construir_user_prompt_campos(esqueleto: str) -> str:
    return f"DOCUMENT SKELETON:\n---\n{esqueleto}\n---\nIdentify every variable campo in this filled template."


async def detectar_campos_plantilla(docx_bytes: bytes, formato: str = "docx") -> tuple[list[dict], list[str]]:
    """Detect fillable campos in a filled example DOCX or XLSX. Never raises.

    extraer_esqueleto → LLM structured call → `validar_campos` deterministic
    filter. On any failure returns ``([], [aviso])`` — graceful degradation,
    same philosophy as `inferir_estructura_plantilla`. ``formato="xlsx"`` swaps
    in the `xlsx_clone_service` twin (sheet!cell addresses, phase 4).
    """
    from app.adapters.llm import get_llm
    from app.core.config import settings
    from app.schemas.agent import LLMMessage

    if formato == "xlsx":
        from app.services.xlsx_clone_service import (
            extraer_esqueleto_xlsx as extraer_esqueleto,
        )
        from app.services.xlsx_clone_service import (
            validar_campos_xlsx as validar_campos,
        )
    else:
        from app.services.docx_clone_service import extraer_esqueleto, validar_campos

    aviso_fallo = "No se pudo detectar campos rellenables en la plantilla; no estará disponible la clonación."
    try:
        esqueleto = extraer_esqueleto(docx_bytes)
    except Exception as exc:
        await logger.awarning("detectar_campos_plantilla_esqueleto_error", error=str(exc)[:200])
        return [], [aviso_fallo]
    if not esqueleto.strip():
        return [], [aviso_fallo]

    messages = [
        LLMMessage(role="system", content=_CAMPOS_PLANTILLA_SYSTEM),
        LLMMessage(role="user", content=_construir_user_prompt_campos(esqueleto)),
    ]
    llm = get_llm(model=settings.LLM_EXTRACTION_MODEL or None)
    try:
        # 16384, not the 4096 the sibling extractions use: the response echoes every
        # valor_ejemplo VERBATIM from a skeleton of up to 14k chars, so real templates
        # (supervision informes with long justificacion rows) overflow 4096 and the
        # truncated JSON fails validation → zero campos detected.
        resp = await llm.complete(
            messages,
            temperature=0.0,
            max_tokens=16384,
            response_format=CamposPlantillaLLM,
        )
        parsed = CamposPlantillaLLM.model_validate_json(resp.content)
    except Exception as exc:
        await logger.awarning("detectar_campos_plantilla_llm_error", error=str(exc)[:200])
        return [], [aviso_fallo]

    try:
        campos, avisos = validar_campos(docx_bytes, [c.model_dump() for c in parsed.campos])
    except Exception as exc:
        await logger.awarning("detectar_campos_plantilla_validar_error", error=str(exc)[:200])
        return [], [aviso_fallo]

    await logger.ainfo("detectar_campos_plantilla_ok", detectados=len(campos), descartados=len(avisos))
    return campos, avisos


# Only these DocumentoFuente types are valid ingestion targets for a per-organism
# template structure — billing-resilience-templates, slice #7, task 7.5b (carry-over
# from slice #5 verify-report WARNING + SUGGESTION b): a CEDULA/RUT/etc. must never
# be storable as a plantilla outside this documented domain.
# `PLANTILLA` (the generic entity-template type) maps to the COEMPRESAR-style
# cuenta-de-cobro letter family ("cuenta_cobro" tipo_documento) — docx clone, phase 2.
_TIPOS_PLANTILLA_VALIDOS = {
    TipoDocumentoFuente.INFORME_ACTIVIDADES,
    TipoDocumentoFuente.INFORME_SUPERVISION,
    TipoDocumentoFuente.PLANTILLA,
}


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
            "(se esperaba informe_actividades, informe_supervision o plantilla); "
            "no se puede ingerir como plantilla de organismo."
        )

    storage = get_storage(settings.S3_BUCKET_DOCUMENTOS)
    content = await storage.download(doc.storage_key)

    # An .xlsx generic template = the Documento Soporte spreadsheet family
    # (xlsx clone, phase 4). No docx-structure extraction applies — a
    # spreadsheet has no columnas/secciones/anexo_refs; only campo detection.
    es_xlsx = doc.tipo == TipoDocumentoFuente.PLANTILLA and doc.nombre.lower().endswith(".xlsx")
    if es_xlsx:
        estructura = EstructuraPlantillaLLM()
    else:
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
    if doc.tipo == TipoDocumentoFuente.PLANTILLA:
        # Generic entity template: xlsx = Documento Soporte spreadsheet,
        # docx = the COEMPRESAR cuenta-de-cobro letter family.
        tipo_documento = "documento_soporte" if es_xlsx else "cuenta_cobro"
    else:
        tipo_documento = doc.tipo.value if doc.tipo else "informe_actividades"
    formato = "xlsx" if es_xlsx else ("docx" if doc.nombre.lower().endswith(".docx") else "pdf")

    # Clonable-campo detection (phases 2/4): only meaningful for DOCX/XLSX
    # sources — `rellenar`/`rellenar_xlsx` clone the original bytes.
    campos: list[dict] = []
    avisos_campos: list[str] = []
    if formato in ("docx", "xlsx"):
        campos, avisos_campos = await detectar_campos_plantilla(content, formato=formato)

    result = await db.execute(
        select(PlantillaOrganismo).where(
            PlantillaOrganismo.usuario_id == usuario_id,
            PlantillaOrganismo.entidad_normalizada == entidad_normalizada,
            PlantillaOrganismo.tipo_documento == tipo_documento,
        )
    )
    plantilla = result.scalar_one_or_none()
    estructura_dict = {
        **estructura.model_dump(),
        "campos": campos,
        "clonable": bool(campos),
        "avisos": avisos_campos,
    }
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


async def listar_plantillas_organismo(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
) -> list[PlantillaOrganismo]:
    """Every ingested plantilla for the contrato's organism, ownership-checked."""
    contrato = await _get_contrato_con_ownership(db, usuario_id, contrato_id)
    if not contrato.entidad or not contrato.entidad.strip():
        return []
    result = await db.execute(
        select(PlantillaOrganismo)
        .where(
            PlantillaOrganismo.usuario_id == usuario_id,
            PlantillaOrganismo.entidad_normalizada == normalize(contrato.entidad),
        )
        .order_by(PlantillaOrganismo.tipo_documento)
    )
    return list(result.scalars().all())


# ── Archive ingestion (formato-replicacion-inteligente, slice 1) ─────────────
#
# Lets a user upload a `.zip`/`.rar` bundle of entity templates in one shot
# instead of one file per tipo_documento. `analizar_archivo_organismo` expands
# the archive (reusing `document_parser.iter_archive_members` — no ad-hoc
# walker), persists each eligible (docx/xlsx/pdf) member as an ordinary
# DocumentoFuente (tipo=PLANTILLA placeholder), and proposes a tipo_documento
# per member via one batched LLM call. Nothing is ingested as a
# PlantillaOrganismo until `confirmar_archivo_organismo` runs, which maps
# each confirmed member back to `ingerir_plantilla_organismo` — UNCHANGED —
# under a bounded concurrency cap.

_TIPO_ARCHIVO_A_ENUM: dict[str, TipoDocumentoFuente] = {
    "informe_actividades": TipoDocumentoFuente.INFORME_ACTIVIDADES,
    "informe_supervision": TipoDocumentoFuente.INFORME_SUPERVISION,
    "cuenta_cobro": TipoDocumentoFuente.PLANTILLA,
    "documento_soporte": TipoDocumentoFuente.PLANTILLA,
}

_ELEGIBLE_EXTENSIONS = {".docx", ".xlsx", ".pdf"}
_CONFIANZA_MINIMA = 0.5
# ponytail: fixed cap 3, make configurable if provider rate limits bite.
_INGEST_CONCURRENCY = 3

_CLASIFICACION_ARCHIVO_SYSTEM = """\
Eres un experto en documentos de organismos contratantes colombianos. Recibís \
una lista de archivos extraídos de un comprimido, cada uno con su nombre y un \
fragmento de su texto. Para cada archivo, proponé el tipo_documento que mejor \
describe su contenido, de este vocabulario cerrado:
- informe_actividades: informe de actividades del contratista
- informe_supervision: informe de supervisión del contrato
- cuenta_cobro: carta/plantilla de cuenta de cobro
- documento_soporte: planilla/documento soporte de pago

Si no podés proponer un tipo con razonable confianza, dejá tipo_documento en \
null. Respondé confianza entre 0 y 1. Respondé ÚNICAMENTE el JSON del esquema.
"""


def _snippet_miembro(filename: str, content: bytes) -> str:
    from app.agent.tools.document_parser import parse_document

    try:
        texto = parse_document(content, filename)
    except Exception:
        return ""
    return texto[:300].strip()


async def clasificar_miembros_archivo(
    miembros: list[tuple[str, bytes]],
) -> tuple[dict[str, MiembroClasificadoLLM], list[str]]:
    """Batched LLM classification of archive members into the closed
    tipo_documento vocabulary. Never raises — degrades to an empty proposal
    map + aviso on any LLM failure, same philosophy as the other extraction
    functions in this module.

    Members whose extension isn't docx/xlsx/pdf are excluded before the LLM
    call — this includes executables/scripts (threat matrix: executable-file
    classification), never classified, never proposed.
    """
    from app.adapters.llm import get_llm
    from app.core.config import settings
    from app.schemas.agent import LLMMessage

    avisos: list[str] = []
    elegibles: list[tuple[str, bytes]] = []
    for filename, content in miembros:
        if Path(filename).suffix.lower() in _ELEGIBLE_EXTENSIONS:
            elegibles.append((filename, content))
        else:
            avisos.append(f"'{filename}' no es un formato soportado (docx/xlsx/pdf); se omitió.")

    if not elegibles:
        return {}, avisos

    listado = "\n".join(f"- {filename}: {_snippet_miembro(filename, content)}" for filename, content in elegibles)
    messages = [
        LLMMessage(role="system", content=_CLASIFICACION_ARCHIVO_SYSTEM),
        LLMMessage(role="user", content=f"ARCHIVOS:\n{listado}"),
    ]
    llm = get_llm(model=settings.LLM_EXTRACTION_MODEL or None)
    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=4096, response_format=ClasificacionArchivoLLM)
        parsed = ClasificacionArchivoLLM.model_validate_json(resp.content)
    except Exception as exc:
        await logger.awarning("clasificar_miembros_archivo_llm_error", error=str(exc)[:200])
        avisos.append("No se pudo clasificar los miembros del archivo automáticamente; asigná el tipo manualmente.")
        # RES-001: a transient LLM failure must not strand the archive — every
        # eligible member still gets uploaded and proposed, just with no
        # suggested tipo (the confirm UI already requires a manual pick for
        # any null-tipo proposal, so this degrades to that same path).
        fallback = {fn: MiembroClasificadoLLM(filename=fn, tipo_documento=None, confianza=0.0) for fn, _c in elegibles}
        return fallback, avisos

    por_filename = {m.filename: m for m in parsed.miembros}
    resultado: dict[str, MiembroClasificadoLLM] = {}
    for filename, _content in elegibles:
        propuesta = por_filename.get(filename)
        if propuesta is None or propuesta.confianza < _CONFIANZA_MINIMA:
            resultado[filename] = MiembroClasificadoLLM(
                filename=filename, tipo_documento=None, confianza=propuesta.confianza if propuesta else 0.0
            )
        else:
            resultado[filename] = propuesta

    await logger.ainfo(
        "clasificar_miembros_archivo_ok", elegibles=len(elegibles), omitidos=len(miembros) - len(elegibles)
    )
    return resultado, avisos


async def analizar_archivo_organismo(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    filename: str,
    content: bytes,
) -> tuple[list[MiembroPropuesto], list[str]]:
    """Expand a `.zip`/`.rar` archive, persist each eligible member as a
    DocumentoFuente (tipo=PLANTILLA placeholder), and return an AI-proposed
    tipo_documento per member. Never ingests a PlantillaOrganismo — only
    `confirmar_archivo_organismo` does that."""
    from app.agent.tools import document_parser
    from app.services import document_service

    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    if Path(filename).suffix.lower() == ".rar" and not (shutil.which("unrar") or shutil.which("bsdtar")):
        return [], ["El soporte para archivos .rar no está disponible en este servidor; usá .zip."]

    miembros = list(document_parser.iter_archive_members(content, filename))
    if not miembros:
        return [], ["El archivo comprimido está vacío; no se ingirió ningún documento."]

    avisos: list[str] = []
    if len(miembros) >= document_parser._ARCHIVE_MAX_MEMBERS:
        avisos.append(
            f"El archivo excede el límite de {document_parser._ARCHIVE_MAX_MEMBERS} miembros; "
            "se procesaron solo los primeros."
        )

    propuestas_llm, avisos_clasificacion = await clasificar_miembros_archivo(miembros)
    avisos.extend(avisos_clasificacion)

    propuestas: list[MiembroPropuesto] = []
    for member_filename, member_bytes in miembros:
        clasif = propuestas_llm.get(member_filename)
        if clasif is None:
            continue  # not eligible (extension) — already avisado above
        doc_result = await document_service.upload_document(
            db=db,
            user_id=usuario_id,
            filename=Path(member_filename).name,
            content=member_bytes,
            content_type="application/octet-stream",
            tipo=TipoDocumentoFuente.PLANTILLA,
            contrato_id=contrato_id,
        )
        propuestas.append(
            MiembroPropuesto(
                documento_fuente_id=doc_result.id,
                filename=member_filename,
                tipo_propuesto=clasif.tipo_documento,
                confianza=clasif.confianza,
            )
        )

    return propuestas, avisos


async def confirmar_archivo_organismo(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    miembros: list[MiembroConfirmado],
) -> tuple[list[MiembroIngestaResultado], list[str]]:
    """Bounded-concurrency ingestion of user-confirmed archive members through
    the existing single-member `ingerir_plantilla_organismo`. Rejects the
    whole batch if two members share a `tipo_documento` (avoids a
    nondeterministic UPSERT race on PlantillaOrganismo's unique constraint).

    Each concurrent ingestion runs on its OWN AsyncSession (same engine as
    `db`, via `async_sessionmaker`) — AsyncSession is not safe for concurrent
    use by multiple coroutines, so the shared `db` is only used for the
    up-front checks.
    """
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    tipos_vistos: dict[str, list[str]] = {}
    for m in miembros:
        tipos_vistos.setdefault(m.tipo_documento, []).append(str(m.documento_fuente_id))
    duplicados = {tipo: ids for tipo, ids in tipos_vistos.items() if len(ids) > 1}
    if duplicados:
        detalle = "; ".join(f"{tipo}: {', '.join(ids)}" for tipo, ids in duplicados.items())
        raise DomainValidationError(
            f"Dos o más miembros tienen el mismo tipo_documento en este lote: {detalle}. Reasigná antes de confirmar."
        )

    session_factory = async_sessionmaker(bind=db.bind, class_=AsyncSession, expire_on_commit=False)
    resultados = await _ingerir_miembros_bounded(session_factory, usuario_id, contrato_id, miembros)
    avisos_globales = [a for r in resultados for a in r.avisos]
    return resultados, avisos_globales


async def _ingerir_miembros_bounded(
    session_factory: async_sessionmaker[AsyncSession],
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    miembros: list[MiembroConfirmado],
) -> list[MiembroIngestaResultado]:
    """The bounded-concurrency gather itself, extracted so the concurrency
    cap can be tested in isolation from the batch-level duplicate-tipo guard
    (only 4 tipo_documento values exist, so a realistic 10-member confirm
    batch always includes a caller-side duplicate check upstream of here)."""
    semaforo = asyncio.Semaphore(_INGEST_CONCURRENCY)

    async def _intentar_ingerir(m: MiembroConfirmado, member_db: AsyncSession) -> MiembroIngestaResultado:
        """One ingest attempt for `m` on `member_db`. Raises `IntegrityError` on
        a concurrent-insert collision on the intermediate derived key
        `ingerir_plantilla_organismo` upserts on — the caller retries."""
        doc = await member_db.get(DocumentoFuente, m.documento_fuente_id)
        filename = doc.nombre if doc is not None else str(m.documento_fuente_id)
        if doc is None or doc.usuario_id != usuario_id:
            return MiembroIngestaResultado(
                documento_fuente_id=m.documento_fuente_id,
                filename=filename,
                persistida=False,
                avisos=["El documento no existe o no pertenece al usuario."],
            )
        doc.tipo = _TIPO_ARCHIVO_A_ENUM[m.tipo_documento]
        await member_db.flush()
        plantilla, avisos = await ingerir_plantilla_organismo(member_db, usuario_id, contrato_id, m.documento_fuente_id)
        # `ingerir_plantilla_organismo` (unchanged) derives cuenta_cobro vs
        # documento_soporte from the file extension for tipo=PLANTILLA docs —
        # the user's CONFIRMED tipo_documento must win over that derivation,
        # so it's applied here, in the same transaction.
        if plantilla is not None and plantilla.tipo_documento != m.tipo_documento:
            # Newest wins: the confirmed tipo_documento may already be occupied
            # by another PlantillaOrganismo for this organismo (unique on
            # usuario_id+entidad_normalizada+tipo_documento). The just-ingested
            # plantilla supersedes it — delete the old row rather than raising
            # an IntegrityError.
            conflicto = await member_db.execute(
                select(PlantillaOrganismo).where(
                    PlantillaOrganismo.usuario_id == usuario_id,
                    PlantillaOrganismo.entidad_normalizada == plantilla.entidad_normalizada,
                    PlantillaOrganismo.tipo_documento == m.tipo_documento,
                    PlantillaOrganismo.id != plantilla.id,
                )
            )
            existente = conflicto.scalar_one_or_none()
            if existente is not None:
                await member_db.delete(existente)
                await member_db.flush()
            plantilla.tipo_documento = m.tipo_documento
            await member_db.flush()
        await member_db.commit()
        return MiembroIngestaResultado(
            documento_fuente_id=m.documento_fuente_id,
            filename=filename,
            persistida=plantilla is not None,
            avisos=avisos,
        )

    async def _ingerir_uno(m: MiembroConfirmado) -> MiembroIngestaResultado:
        async with semaforo, session_factory() as member_db:
            filename = str(m.documento_fuente_id)
            for intento in range(2):
                try:
                    return await _intentar_ingerir(m, member_db)
                except DomainError as exc:
                    await member_db.rollback()
                    await logger.awarning(
                        "archivo_confirmar_miembro_fallo",
                        documento_fuente_id=str(m.documento_fuente_id),
                        error=exc.detail,
                    )
                    return MiembroIngestaResultado(
                        documento_fuente_id=m.documento_fuente_id,
                        filename=filename,
                        persistida=False,
                        avisos=[exc.detail],
                    )
                except IntegrityError:
                    # REL-001: two members whose ingest derives the same
                    # intermediate (usuario, entidad, tipo) key can both reach
                    # the SELECT-then-INSERT in `ingerir_plantilla_organismo`
                    # before either commits, under Semaphore(3) concurrency —
                    # the loser's flush/commit raises IntegrityError. Rollback
                    # and retry ONCE: by then the winner's row is committed, so
                    # the retry's SELECT finds it and takes the UPDATE branch.
                    await member_db.rollback()
                    if intento == 0:
                        await logger.awarning(
                            "archivo_confirmar_miembro_colision_concurrente",
                            documento_fuente_id=str(m.documento_fuente_id),
                        )
                        continue
                    await logger.awarning(
                        "archivo_confirmar_miembro_colision_persistente",
                        documento_fuente_id=str(m.documento_fuente_id),
                    )
                    return MiembroIngestaResultado(
                        documento_fuente_id=m.documento_fuente_id,
                        filename=filename,
                        persistida=False,
                        avisos=["No se pudo ingerir: conflicto de concurrencia con otro miembro del lote."],
                    )
                except Exception as exc:
                    # RES-002: any other failure (storage errors, etc.) must
                    # degrade to a per-member result, not abort the batch.
                    await member_db.rollback()
                    await logger.awarning(
                        "archivo_confirmar_miembro_error_inesperado",
                        documento_fuente_id=str(m.documento_fuente_id),
                        error=str(exc)[:200],
                    )
                    return MiembroIngestaResultado(
                        documento_fuente_id=m.documento_fuente_id,
                        filename=filename,
                        persistida=False,
                        avisos=["No se pudo ingerir la plantilla debido a un error inesperado."],
                    )
            raise AssertionError("unreachable")  # pragma: no cover — loop always returns or raises above

    resultados_o_excepciones = await asyncio.gather(*(_ingerir_uno(m) for m in miembros), return_exceptions=True)
    resultados: list[MiembroIngestaResultado] = []
    for m, r in zip(miembros, resultados_o_excepciones, strict=True):
        if isinstance(r, BaseException):
            # Defensive: should be unreachable (every path inside _ingerir_uno
            # already returns a MiembroIngestaResultado), but a stray exception
            # here must still degrade to a per-member failure, never abort gather.
            await logger.awarning(
                "archivo_confirmar_miembro_excepcion_no_capturada",
                documento_fuente_id=str(m.documento_fuente_id),
                error=str(r)[:200],
            )
            resultados.append(
                MiembroIngestaResultado(
                    documento_fuente_id=m.documento_fuente_id,
                    filename=str(m.documento_fuente_id),
                    persistida=False,
                    avisos=["No se pudo ingerir la plantilla debido a un error inesperado."],
                )
            )
        else:
            resultados.append(r)
    return resultados
