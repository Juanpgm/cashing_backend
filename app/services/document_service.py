"""Document processing service — upload, parse, and process documents."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.storage import get_storage as _get_storage
from app.agent.tools.contract_parser import (
    extract_obligaciones_verbatim as _extract_obligaciones_verbatim,
)
from app.agent.tools.contract_parser import (
    extract_obligation_sections as _extract_obligation_sections,
)
from app.agent.tools.contract_parser import (
    obligacion_items_to_extraidas as _obligacion_items_to_extraidas,
)
from app.agent.tools.contract_parser import (
    parse_obligaciones_structured as _parse_obligaciones_structured,
)
from app.agent.tools.document_parser import parse_document
from app.agent.tools.multimodal_parser import (
    build_multimodal_content_parts,
    guess_mime_type,
    is_multimodal_supported,
    is_text_sufficient,
)
from app.agent.tools.ocr import extract_text as ocr_extract_text
from app.agent.tools.ocr import ocr_available
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.core.file_validation import get_safe_filename
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.plantilla import Plantilla, TipoPlantilla
from app.schemas.agent import (
    ContratoExtractionResult,
    ContratoExtraido,
    DocumentProcessResponse,
    DocumentUploadResponse,
    LLMMessage,
    ObligacionesLLMList,
    ObligacionExtraida,
)
from app.schemas.documento_fuente import ContratoConfiguracionResponse, DocumentoFuenteResponse

logger = structlog.get_logger("services.document")


async def _obtener_obligaciones_existentes(
    contrato_id: uuid.UUID,
    db: AsyncSession,
) -> list[ObligacionExtraida]:
    """Load obligations already stored in DB for a contract."""
    result = await db.execute(
        select(Obligacion).where(Obligacion.contrato_id == contrato_id).order_by(Obligacion.orden)
    )
    return [
        ObligacionExtraida(
            descripcion=ob.descripcion,
            tipo=ob.tipo.value,
            orden=ob.orden,
        )
        for ob in result.scalars().all()
    ]


async def _extraer_obligaciones(
    texto_contrato: str,
    contrato_id: uuid.UUID | None,
    db: AsyncSession,
) -> tuple[list[ObligacionExtraida], list[str]]:
    """Call LLM to extract obligations and return extracted list + warnings.

    When ``contrato_id`` is provided, new obligations are persisted to DB.
    When ``contrato_id`` is None (auto-create failed), obligations are extracted
    for display only — no DB persistence.

    Returns ``(obligations, warnings)`` where warnings tracks LLM/parsing issues.
    """
    from app.adapters.llm import get_llm
    from app.agent.prompts.obligaciones import OBLIGACIONES_SYSTEM, OBLIGACIONES_USER

    avisos: list[str] = []
    # Use dedicated extraction model if configured (e.g. local Ollama), else default
    extraction_model = settings.LLM_EXTRACTION_MODEL or None

    # ── Step 1: try the deterministic verbatim extractor first ─────────────
    # This preserves the EXACT wording of each obligation as it appears in
    # the contract, instead of the LLM-paraphrased version.
    verbatim = _extract_obligaciones_verbatim(texto_contrato)
    if verbatim:
        await logger.ainfo(
            "obligaciones_verbatim",
            contrato_id=str(contrato_id),
            total=len(verbatim),
            total_chars=len(texto_contrato),
        )
        extraidas = verbatim
        return await _persist_obligaciones(extraidas, contrato_id, db, avisos)

    # ── Step 2: fall back to LLM-based extraction ──────────────────────────
    llm = get_llm(model=extraction_model)

    chunks = _extract_obligation_sections(texto_contrato)
    await logger.ainfo(
        "obligaciones_chunks",
        contrato_id=str(contrato_id),
        total_chunks=len(chunks),
        total_chars=len(texto_contrato),
        chunk_sizes=[len(c) for c in chunks],
        model=extraction_model or settings.LLM_DEFAULT_MODEL,
    )

    llm_errors = 0
    first_error_hint: str = ""

    async def _process_chunk(i: int, chunk: str) -> list[ObligacionExtraida]:
        nonlocal llm_errors, first_error_hint
        messages = [
            LLMMessage(role="system", content=OBLIGACIONES_SYSTEM),
            LLMMessage(role="user", content=OBLIGACIONES_USER.format(texto_contrato=chunk)),
        ]
        try:
            resp = await llm.complete(messages, temperature=0.0, max_tokens=4096, response_format=ObligacionesLLMList)
        except Exception as exc:
            llm_errors += 1
            hint = str(exc)[:300]
            if not first_error_hint:
                first_error_hint = hint
            await logger.awarning(
                "obligaciones_llm_chunk_failed",
                contrato_id=str(contrato_id),
                chunk=i,
                error=hint,
            )
            return []

        chunk_obs = _parse_obligaciones_structured(resp.content)
        if not chunk_obs:
            await logger.awarning(
                "obligaciones_parse_zero",
                contrato_id=str(contrato_id),
                chunk=i,
                raw_response=resp.content[:500],
            )
        await logger.ainfo(
            "obligaciones_chunk_done",
            contrato_id=str(contrato_id),
            chunk=i,
            found=len(chunk_obs),
            tokens=resp.total_tokens,
        )
        return chunk_obs

    chunk_results = await asyncio.gather(*[_process_chunk(i, chunk) for i, chunk in enumerate(chunks)])

    # Deduplicate after gathering all results (parallel runs share no state)
    all_raw: list[ObligacionExtraida] = []
    seen_norm: set[str] = set()
    for chunk_obs in chunk_results:
        for ob in chunk_obs:
            norm = ob.descripcion.lower().strip()
            if norm not in seen_norm:
                seen_norm.add(norm)
                all_raw.append(ob)

    if llm_errors > 0:
        cause = (
            f" Causa: {first_error_hint}"
            if first_error_hint
            else " Verifica la configuración del modelo LLM (API key, cuota, conectividad)."
        )
        avisos.append(f"La extracción de obligaciones falló en {llm_errors}/{len(chunks)} fragmentos.{cause}")

    extraidas = all_raw
    if not extraidas:
        await logger.awarning(
            "obligaciones_llm_empty",
            contrato_id=str(contrato_id),
            chunks_processed=len(chunks),
        )
        if llm_errors == 0:
            avisos.append(
                "No se encontraron obligaciones específicas en el documento. "
                "Verifica que el PDF contenga una sección de obligaciones del contratista."
            )
        return [], avisos

    return await _persist_obligaciones(extraidas, contrato_id, db, avisos)


async def extraer_obligaciones_texto(
    texto_contrato: str,
    contrato_id: uuid.UUID | None,
    db: AsyncSession,
) -> tuple[list[ObligacionExtraida], list[str]]:
    """Public entry point: extract and persist obligations from raw contract text.

    Tries the deterministic verbatim extractor first, falls back to LLM chunking.
    When ``contrato_id`` is provided, results are upserted to the DB.
    Returns ``(obligations, warnings)``.
    """
    return await _extraer_obligaciones(texto_contrato, contrato_id, db)


async def _persist_obligaciones(
    extraidas: list[ObligacionExtraida],
    contrato_id: uuid.UUID | None,
    db: AsyncSession,
    avisos: list[str],
) -> tuple[list[ObligacionExtraida], list[str]]:
    """Upsert extracted obligations into the DB (or return as-is if no contrato).

    Deduplicates by normalized (lowercased+stripped) descripcion against any
    obligations already attached to ``contrato_id``. New rows are appended
    with an ``orden`` continuing the existing sequence.
    """
    # When contrato_id is None (auto-create failed), return extracted
    # obligations for display only — no DB persistence.
    if contrato_id is None:
        return extraidas, avisos

    existing_result = await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato_id))
    existing_obs = existing_result.scalars().all()
    existing_norm = {ob.descripcion.lower().strip(): ob for ob in existing_obs}

    next_orden = max((ob.orden for ob in existing_obs), default=0) + 1

    nuevas = 0
    for ob in extraidas:
        norm_key = ob.descripcion.lower().strip()
        if norm_key in existing_norm:
            existing_ob = existing_norm[norm_key]
            if existing_ob.tipo.value != ob.tipo:
                existing_ob.tipo = TipoObligacion(ob.tipo)
        else:
            db.add(
                Obligacion(
                    contrato_id=contrato_id,
                    descripcion=ob.descripcion,
                    tipo=TipoObligacion(ob.tipo),
                    orden=next_orden,
                    etiqueta=ob.etiqueta,
                )
            )
            nuevas += 1
            next_orden += 1

    await db.flush()
    await logger.ainfo(
        "obligaciones_extraidas",
        contrato_id=str(contrato_id),
        total_extraidas=len(extraidas),
        nuevas_insertadas=nuevas,
        ya_existentes=len(extraidas) - nuevas,
    )
    return extraidas, avisos


def _safe_decimal(val: str | None) -> Decimal:
    if not val:
        return Decimal("0.00")
    cleaned = val.replace(" ", "").replace("$", "")
    if "," in cleaned and "." in cleaned:
        if cleaned.rindex(",") > cleaned.rindex("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        cleaned = cleaned.replace(",", ".") if len(parts) == 2 and len(parts[1]) <= 2 else cleaned.replace(",", "")
    else:
        cleaned = cleaned.replace(".", "", cleaned.count(".") - 1) if cleaned.count(".") > 1 else cleaned
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return Decimal("0.00")


def _safe_date(val: str | None) -> date | None:
    if not val:
        return None
    try:
        return date.fromisoformat(val.strip())
    except ValueError:
        return None


def _build_contrato_extraido(campos: dict[str, str]) -> ContratoExtraido | None:
    """Convert a campos dict (from LLM parsing) to a ContratoExtraido schema."""
    if not campos.get("numero_contrato") and not campos.get("objeto"):
        return None
    return ContratoExtraido(
        numero_contrato=campos.get("numero_contrato", "SIN-NUMERO"),
        objeto=campos.get("objeto", "Objeto pendiente de revisión"),
        valor_total=_safe_decimal(campos.get("valor_total")),
        valor_mensual=_safe_decimal(campos.get("valor_mensual")),
        fecha_inicio=_safe_date(campos.get("fecha_inicio")),
        fecha_fin=_safe_date(campos.get("fecha_fin")),
        supervisor_nombre=campos.get("supervisor_nombre"),
        cargo_supervisor=campos.get("cargo_supervisor"),
        entidad=campos.get("entidad"),
        dependencia=campos.get("dependencia"),
        documento_proveedor=campos.get("documento_proveedor"),
        pais=campos.get("pais"),
        departamento=campos.get("departamento"),
        ciudad=campos.get("ciudad"),
        direccion_ejecucion=campos.get("direccion_ejecucion"),
    )


async def _extraer_datos_contrato(
    texto_contrato: str,
) -> tuple[ContratoExtraido | None, list[str]]:
    """Delegate contract metadata extraction to the agent graph.

    Returns (contrato, avisos). On any failure returns (None, [reason]).
    Never raises — errors are captured as avisos.
    """
    from app.schemas.agent import AgentMode
    from app.services.agent_service import get_graph

    state = {
        "user_input": "__extract_contract__",
        "mode": AgentMode.EXTRACT_OBLIGATIONS,
        "texto_contrato": texto_contrato,
        "contrato_id_str": None,  # None = also extract metadata
    }
    try:
        result = await get_graph().ainvoke(state)
    except Exception as exc:
        await logger.awarning("extraer_datos_contrato_graph_failed", error=str(exc)[:400])
        return None, [f"Error interno al invocar el agente de extracción: {str(exc)[:200]}"]

    # Surface any avisos the extraction node produced (LLM errors, parse failures, etc.)
    graph_avisos: list[str] = list(result.get("extraction_avisos") or [])

    raw = result.get("contrato_extraido")
    if not raw:
        if not graph_avisos:
            graph_avisos.append(
                "El agente no pudo identificar número de contrato ni objeto en el documento. "
                "Verifica que el PDF contenga el texto del contrato completo y legible."
            )
        return None, graph_avisos

    contrato = _build_contrato_extraido(raw)
    if contrato is None:
        graph_avisos.append(
            "Se extrajo texto del contrato pero no se encontró número ni objeto. "
            "Verifica que el documento sea un contrato de prestación de servicios colombiano."
        )
    return contrato, graph_avisos


# Vision models known to be decommissioned by their provider. They are skipped
# in the chain (with a warning) so a stale config value cannot silently break the
# whole vision path — providers retire preview models regularly, and the symptom
# is "no data extracted" with no obvious cause.
_DECOMMISSIONED_VISION_MODELS: frozenset[str] = frozenset(
    {
        "groq/llama-3.2-11b-vision-preview",
        "groq/llama-3.2-90b-vision-preview",
        "groq/llava-v1.5-7b-4096-preview",
    }
)

# Current, vision-capable fallback models tried (in order) after the configured
# model, so one unavailable provider (missing key, depleted quota, decommissioned
# model) does not leave the user with no extraction at all.
_VISION_FALLBACK_MODELS: tuple[str, ...] = (
    "gemini/gemini-2.5-flash-lite",  # reads PDF natively, generous free tier
    "groq/meta-llama/llama-4-scout-17b-16e-instruct",  # current Groq vision model (rasterizes PDF, <=5 pages)
)


def _vision_model_has_credentials(model: str) -> bool:
    """True when the provider key needed for this vision model is configured.

    Local (Ollama) and unknown providers are assumed usable — only cloud
    providers that need an API key are filtered out when the key is absent.
    """
    if model.startswith("gemini/"):
        return bool(settings.GEMINI_API_KEY)
    if model.startswith("groq/"):
        return bool(settings.GROQ_API_KEY)
    if model.startswith(("openai/", "gpt-")):
        return bool(settings.OPENAI_API_KEY)
    if model.startswith("mistral/"):
        return bool(settings.MISTRAL_API_KEY)
    return True


def _vision_model_chain() -> list[str]:
    """Ordered, de-duplicated list of usable vision models to try.

    Starts with the configured ``LLM_MULTIMODAL_MODEL``, then appends curated
    current fallbacks. Skips decommissioned models and any whose provider key is
    missing, so we never waste a call on a model that cannot possibly succeed.
    """
    candidates = [settings.LLM_MULTIMODAL_MODEL, *_VISION_FALLBACK_MODELS]
    chain: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        if not model or model in seen:
            continue
        seen.add(model)
        if model in _DECOMMISSIONED_VISION_MODELS:
            continue
        if not _vision_model_has_credentials(model):
            continue
        chain.append(model)
    return chain


async def _extraer_contrato_multimodal(
    content: bytes,
    mime_type: str,
) -> ContratoExtractionResult | None:
    """Extract contract data directly from a scanned PDF or image via a vision model.

    The hybrid fallback path: when text extraction yields too little text, the
    vision model reads the file itself (acting as OCR) and returns the contract
    metadata, its specific obligations and a plain-text transcription, all in a
    single structured call.

    Resilient by design: it tries a chain of vision models (configured model first,
    then curated current fallbacks). One provider being down — no key, depleted
    quota, or a decommissioned model — falls through to the next instead of
    failing the whole extraction. Returns ``None`` only when every model fails
    (caller adds an aviso).
    """
    if not is_multimodal_supported(mime_type):
        return None

    from app.adapters.llm import get_llm
    from app.agent.prompts.multimodal_extraction import (
        MULTIMODAL_EXTRACTION_SYSTEM,
        MULTIMODAL_EXTRACTION_USER,
    )

    chain = _vision_model_chain()
    if not chain:
        await logger.awarning(
            "multimodal_no_usable_model",
            configured=settings.LLM_MULTIMODAL_MODEL,
            note="No vision model has usable credentials. Set GEMINI_API_KEY or GROQ_API_KEY.",
        )
        return None

    last_error: str | None = None
    for model in chain:
        try:
            # Native-PDF providers (Gemini) get the PDF directly; image-only vision
            # models get the pages rasterized. Rebuilt per model since the part shape
            # depends on the provider. Inside the try so a corrupt PDF or a failed
            # rasterization falls through to the next model instead of crashing the
            # whole upload.
            parts = build_multimodal_content_parts(
                content,
                mime_type,
                model,
                max_pdf_pages=settings.MULTIMODAL_MAX_PDF_PAGES,
                dpi=settings.MULTIMODAL_RASTER_DPI,
            )
            messages = [
                LLMMessage(role="system", content=MULTIMODAL_EXTRACTION_SYSTEM),
                LLMMessage(role="user", content=[{"type": "text", "text": MULTIMODAL_EXTRACTION_USER}, *parts]),
            ]
            # fallback=False: the generic text-only fallback chain cannot read image
            # parts; this function manages its own vision-aware fallback instead.
            resp = await get_llm(model=model).complete(
                messages,
                temperature=0.0,
                max_tokens=8192,
                response_format=ContratoExtractionResult,
                fallback=False,
            )
        except Exception as exc:
            last_error = str(exc)
            await logger.awarning("multimodal_model_failed", model=model, error=str(exc)[:200])
            continue

        try:
            result = ContratoExtractionResult.model_validate_json(resp.content)
        except ValidationError as exc:
            last_error = str(exc)
            await logger.awarning(
                "multimodal_parse_failed",
                model=model,
                error=str(exc)[:200],
                raw=resp.content[:300],
            )
            continue

        await logger.ainfo(
            "multimodal_extraction_ok",
            model=model,
            obligaciones=len(result.obligaciones),
            transcripcion_chars=len(result.transcripcion or ""),
        )
        return result

    await logger.awarning("multimodal_all_models_failed", tried=chain, error=(last_error or "")[:200])
    return None


async def extraer_texto_documento(content: bytes, filename: str) -> tuple[str | None, list[str]]:
    """Best-effort plain-text extraction from an uploaded document.

    Reuses the same ladder the contract upload uses: native text extraction
    (pdfplumber / docx / xlsx) first, then local OCR for scanned PDFs/images
    when the text layer is too thin. Returns ``(texto, avisos)`` — ``texto`` is
    None/empty when nothing readable could be recovered.

    Intended for generic text recovery (e.g. inferring a requirements checklist
    from a pliego de condiciones), independent of the contract-specific vision
    extraction in ``_extraer_contrato_multimodal``.
    """
    avisos: list[str] = []
    texto: str | None = None
    try:
        texto = parse_document(content, filename)
    except Exception as exc:
        await logger.awarning("extraer_texto_parse_failed", filename=filename, error=str(exc))

    if is_text_sufficient(texto, settings.EXTRACTION_MIN_TEXT_CHARS):
        return texto, avisos

    mime = guess_mime_type(filename)
    if (
        settings.EXTRACTION_OCR_ENABLED
        and is_multimodal_supported(mime)
        and ocr_available(settings.EXTRACTION_OCR_ENGINE)
    ):
        try:
            texto_ocr = ocr_extract_text(
                content,
                mime,
                engine=settings.EXTRACTION_OCR_ENGINE,
                lang=settings.EXTRACTION_OCR_LANG,
                max_pages=settings.MULTIMODAL_MAX_PDF_PAGES,
                dpi=settings.MULTIMODAL_RASTER_DPI,
            )
        except Exception as exc:
            texto_ocr = ""
            await logger.awarning("extraer_texto_ocr_failed", filename=filename, error=str(exc))
        if is_text_sufficient(texto_ocr, settings.EXTRACTION_MIN_TEXT_CHARS):
            return texto_ocr, avisos

    if not (texto and texto.strip()):
        avisos.append(
            "No se pudo extraer texto legible del documento. "
            "Subí un PDF con texto seleccionable o pegá el contenido en el cuadro de texto."
        )
    return texto, avisos


_SYSTEM_PROMPT_TEMPLATE = """\
Eres un asistente especializado en contratos de prestación de servicios para el Estado colombiano.
Tu función es ayudar al contratista a redactar cuentas de cobro con actividades y justificaciones \
que demuestren el cumplimiento de sus obligaciones contractuales.

## DATOS DEL CONTRATO
- Número: {numero_contrato}
- Entidad contratante: {entidad}
- Dependencia: {dependencia}
- Supervisor: {supervisor}
- Objeto: {objeto}
- Vigencia: {fecha_inicio} al {fecha_fin}
- Valor total: $ {valor_total}
- Valor mensual: $ {valor_mensual}

## OBLIGACIONES CONTRACTUALES
{obligaciones}

## TEXTO DEL CONTRATO
{texto_contrato}

## INSTRUCCIONES Y DIRECTIVAS DEL USUARIO
{instrucciones}

## REGLAS DE REDACCIÓN
- Redacta actividades en primera persona, pasado, con verbos de acción concretos.
- Cada actividad debe justificarse indicando a qué obligación contractual da cumplimiento.
- Usa lenguaje formal apropiado para documentos oficiales colombianos.
- No inventes datos, fechas ni cifras que no se desprendan del contexto.
- Asegúrate de que el conjunto de actividades cubra todas las obligaciones del contrato para el período.
"""


async def upload_document(
    db: AsyncSession,
    user_id: uuid.UUID,
    filename: str,
    content: bytes,
    content_type: str,
    tipo: TipoDocumentoFuente = TipoDocumentoFuente.CONTRATO,
    contrato_id: uuid.UUID | None = None,
    cuenta_cobro_id: uuid.UUID | None = None,
    requisito_codigo: str | None = None,
) -> DocumentUploadResponse:
    """Upload a document to storage and create a DB record.

    When ``tipo=contrato`` and ``contrato_id`` is not provided, the service
    will use the LLM to extract contract metadata from the document text and
    automatically create a ``Contrato`` record linked to the uploaded document.

    When both ``cuenta_cobro_id`` and ``requisito_codigo`` are provided, the
    uploaded document is linked to the cuenta de cobro checklist row for that
    requisito, transitioning it to estado=CARGADO.
    """
    # If contrato_id is given, verify ownership
    if contrato_id is not None:
        r = await db.execute(
            select(Contrato).where(
                Contrato.id == contrato_id,
                Contrato.usuario_id == user_id,
                Contrato.deleted_at.is_(None),
            )
        )
        if r.scalar_one_or_none() is None:
            raise NotFoundError("Contrato", str(contrato_id))

    # Two-tier scoping. Derive contrato_id from the cuenta up-front so downstream
    # dedup/replace logic sees it, then decide the document's scope: contract-level
    # requisitos (RUT, cédula, contrato, RPC…) produce a SHARED document
    # (cuenta_cobro_id NULL) that fulfils the requisito in every cuenta; everything
    # else stays strictly scoped to this cuenta.
    if contrato_id is None and cuenta_cobro_id is not None:
        cc_lookup = await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_cobro_id))
        cc = cc_lookup.scalar_one_or_none()
        if cc is not None:
            contrato_id = cc.contrato_id

    from app.services.checklist_service import es_nivel_contrato as _es_nivel_contrato

    doc_cuenta_cobro_id = None if (requisito_codigo and _es_nivel_contrato(requisito_codigo)) else cuenta_cobro_id

    # Enforce 1-document-per-contract rule for tipo=CONTRATO.
    # If a CONTRATO document already exists for this contract (any filename),
    # replace it: delete the old file from storage and the old DB record so
    # the new upload becomes the single source of truth.
    if tipo == TipoDocumentoFuente.CONTRATO and contrato_id is not None:
        prev_result = await db.execute(
            select(DocumentoFuente).where(
                DocumentoFuente.contrato_id == contrato_id,
                DocumentoFuente.tipo == TipoDocumentoFuente.CONTRATO,
            )
        )
        prev_docs = list(prev_result.scalars().all())
        for prev_doc in prev_docs:
            # Best-effort delete of the old file; a storage miss must not block
            # replacing the DB record (the new upload is the source of truth).
            with contextlib.suppress(Exception):
                await _get_storage(settings.S3_BUCKET_DOCUMENTOS).delete(prev_doc.storage_key)
            await db.delete(prev_doc)
        if prev_docs:
            await db.flush()
            await logger.ainfo(
                "contrato_documento_replaced",
                replaced=len(prev_docs),
                new_nombre=filename,
                contrato_id=str(contrato_id),
            )

    # Deduplicate: if same filename+tipo+contrato already exists, reuse it.
    # In auto-create mode (contrato_id=None), only short-circuit when the
    # previous upload already linked a contrato successfully.  If it didn't
    # (contrato_id IS NULL on the existing row), fall through and retry extraction
    # so the user can re-upload after fixing their document or model config.
    dup_conditions = [
        DocumentoFuente.usuario_id == user_id,
        DocumentoFuente.nombre == filename,
        DocumentoFuente.tipo == tipo,
    ]
    # Dedup within the document's effective scope: cuenta-level docs dedup per cuenta,
    # contract-level docs dedup per contract (same filename can exist independently
    # across cuentas, but a shared contract-level doc is reused).
    if doc_cuenta_cobro_id is not None:
        dup_conditions.append(DocumentoFuente.cuenta_cobro_id == doc_cuenta_cobro_id)
        if contrato_id is not None:
            dup_conditions.append(DocumentoFuente.contrato_id == contrato_id)
    elif contrato_id is not None:
        dup_conditions.append(DocumentoFuente.cuenta_cobro_id.is_(None))
        dup_conditions.append(DocumentoFuente.contrato_id == contrato_id)
    else:
        dup_conditions.append(DocumentoFuente.contrato_id.is_not(None))
    dup_result = await db.execute(select(DocumentoFuente).where(*dup_conditions).limit(1))
    existing_doc = dup_result.scalar_one_or_none()

    obligaciones_extraidas: list[ObligacionExtraida] = []
    contrato_creado: ContratoExtraido | None = None
    avisos: list[str] = []

    if existing_doc is not None:
        # Document already uploaded — return existing record + obligations
        effective_contrato_id = existing_doc.contrato_id or contrato_id
        await logger.ainfo(
            "document_already_exists",
            doc_id=str(existing_doc.id),
            filename=filename,
            contrato_id=str(effective_contrato_id),
            has_text=bool(existing_doc.texto_extraido),
        )
        avisos.append(
            f"El archivo '{filename}' ya estaba cargado. Se retorna el documento existente. "
            "Si querés reemplazarlo, eliminá el documento anterior y subí el nuevo."
        )
        return DocumentUploadResponse(
            id=existing_doc.id,
            nombre=existing_doc.nombre,
            tipo=existing_doc.tipo.value,
            texto_extraido=existing_doc.texto_extraido,
            contrato_id=effective_contrato_id,
            obligaciones_extraidas=obligaciones_extraidas,
            avisos=avisos,
        )

    # Extract text first (in-memory, no network) so obligations can be parsed
    # even if storage upload fails or is slow.
    texto_extraido: str | None = None
    try:
        texto_extraido = parse_document(content, filename)
    except (ValueError, Exception) as exc:
        await logger.awarning("text_extraction_failed", filename=filename, error=str(exc))

    texto_suficiente = is_text_sufficient(texto_extraido, settings.EXTRACTION_MIN_TEXT_CHARS)

    await logger.ainfo(
        "document_text_extracted",
        filename=filename,
        text_length=len(texto_extraido) if texto_extraido else 0,
        texto_suficiente=texto_suficiente,
        contrato_id=str(contrato_id),
        tipo=tipo if isinstance(tipo, str) else tipo.value,
    )

    # ── OCR tier: scanned PDF/image with no text layer → recover text locally ──
    # before paying for the vision model. The deterministic/text path then runs on
    # the OCR'd text exactly as for a native-text PDF.
    if (
        not texto_suficiente
        and settings.EXTRACTION_OCR_ENABLED
        and is_multimodal_supported(guess_mime_type(filename))
        and ocr_available(settings.EXTRACTION_OCR_ENGINE)
    ):
        try:
            texto_ocr = ocr_extract_text(
                content,
                guess_mime_type(filename),
                engine=settings.EXTRACTION_OCR_ENGINE,
                lang=settings.EXTRACTION_OCR_LANG,
                max_pages=settings.MULTIMODAL_MAX_PDF_PAGES,
                dpi=settings.MULTIMODAL_RASTER_DPI,
            )
        except Exception as exc:
            texto_ocr = ""
            await logger.awarning("ocr_failed", filename=filename, error=str(exc))
        if is_text_sufficient(texto_ocr, settings.EXTRACTION_MIN_TEXT_CHARS):
            texto_extraido = texto_ocr
            texto_suficiente = True
            await logger.ainfo("ocr_recovered_text", filename=filename, chars=len(texto_ocr))

    es_contrato_autocrear = tipo == TipoDocumentoFuente.CONTRATO and contrato_id is None

    # ── Hybrid fallback: scanned PDF / image (poor/no text) → vision extraction ──
    # Runs for ALL CONTRATO uploads with insufficient text — both auto-create and
    # existing-contract uploads. For auto-create, the vision result also populates
    # contract metadata. For existing contracts, only obligations are extracted.
    multimodal_result: ContratoExtractionResult | None = None
    if (
        tipo == TipoDocumentoFuente.CONTRATO
        and not texto_suficiente
        and settings.EXTRACTION_MULTIMODAL_FALLBACK_ENABLED
        and is_multimodal_supported(guess_mime_type(filename))
    ):
        multimodal_result = await _extraer_contrato_multimodal(content, guess_mime_type(filename))
        if multimodal_result is not None:
            texto_extraido = multimodal_result.transcripcion or texto_extraido
            if es_contrato_autocrear:
                contrato_creado = _build_contrato_extraido(
                    {k: v for k, v in multimodal_result.contrato.model_dump().items() if v}
                )
                if contrato_creado is None:
                    avisos.append("No se pudieron extraer datos del contrato desde la imagen/PDF escaneado.")
        else:
            avisos.append(
                "No se pudo procesar la imagen o el PDF escaneado del contrato. "
                "Sube un PDF con texto seleccionable o una imagen más legible."
            )

    # Auto-create contract from the TEXT path when text is usable (no vision result)
    if es_contrato_autocrear and contrato_creado is None and texto_extraido and texto_suficiente:
        contrato_creado, extraccion_avisos = await _extraer_datos_contrato(texto_extraido)
        if contrato_creado is None:
            avisos.extend(extraccion_avisos)

    # Persist the Contrato row from whichever path produced contrato_creado
    if es_contrato_autocrear and contrato_creado is not None:
        from datetime import date as date_type

        # Default documento_proveedor to user cedula when not extracted
        doc_proveedor = contrato_creado.documento_proveedor
        if not doc_proveedor:
            from app.models.usuario import Usuario as _Usuario

            usuario_obj = await db.get(_Usuario, user_id)
            if usuario_obj and usuario_obj.cedula:
                doc_proveedor = usuario_obj.cedula

        contrato = Contrato(
            usuario_id=user_id,
            numero_contrato=contrato_creado.numero_contrato,
            objeto=contrato_creado.objeto,
            valor_total=float(contrato_creado.valor_total),
            valor_mensual=float(contrato_creado.valor_mensual),
            fecha_inicio=contrato_creado.fecha_inicio or date_type.today(),
            fecha_fin=contrato_creado.fecha_fin or date_type.today(),
            supervisor_nombre=contrato_creado.supervisor_nombre,
            cargo_supervisor=contrato_creado.cargo_supervisor,
            entidad=contrato_creado.entidad,
            dependencia=contrato_creado.dependencia,
            documento_proveedor=doc_proveedor,
            pais=contrato_creado.pais,
            departamento=contrato_creado.departamento,
            ciudad=contrato_creado.ciudad,
            direccion_ejecucion=contrato_creado.direccion_ejecucion,
        )
        db.add(contrato)
        await db.flush()
        contrato_id = contrato.id
        await logger.ainfo(
            "contrato_auto_created",
            contrato_id=str(contrato_id),
            numero=contrato_creado.numero_contrato,
            usuario_id=str(user_id),
        )

    # Extract obligations once we have a contract to link them to.
    if tipo == TipoDocumentoFuente.CONTRATO and contrato_id is not None:
        if multimodal_result is not None:
            ob_items = _obligacion_items_to_extraidas(multimodal_result.obligaciones)
            obligaciones_extraidas, ob_avisos = await _persist_obligaciones(ob_items, contrato_id, db, [])
            avisos.extend(ob_avisos)
        elif texto_extraido:
            obligaciones_extraidas, ob_avisos = await _extraer_obligaciones(texto_extraido, contrato_id, db)
            avisos.extend(ob_avisos)

    # Upload to storage (local filesystem or S3, depending on STORAGE_PROVIDER).
    safe_filename = get_safe_filename(filename)
    storage_key = f"usuarios/{user_id}/documentos/{uuid.uuid4()}/{safe_filename}"
    try:
        storage = _get_storage(settings.S3_BUCKET_DOCUMENTOS)
        await storage.upload(key=storage_key, data=content, content_type=content_type)
    except Exception as exc:
        await logger.awarning(
            "storage_upload_failed",
            filename=filename,
            error=str(exc),
        )
        raise

    # When uploading via the checklist flow (cuenta_cobro_id provided, contrato_id not),
    # derive contrato_id from the cuenta so DocumentoFuente stays traceable per contract.
    if contrato_id is None and cuenta_cobro_id is not None:
        cc_lookup = await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_cobro_id))
        cc = cc_lookup.scalar_one_or_none()
        if cc is not None:
            contrato_id = cc.contrato_id

    # Store the safe name; preserve the original in metadata for traceability.
    metadata: dict[str, str] = {}
    if safe_filename != filename:
        metadata["nombre_original"] = filename

    doc = DocumentoFuente(
        usuario_id=user_id,
        contrato_id=contrato_id,
        cuenta_cobro_id=doc_cuenta_cobro_id,
        storage_key=storage_key,
        nombre=safe_filename,
        tipo=tipo,
        texto_extraido=texto_extraido,
        metadata_json=metadata if metadata else None,
    )
    from app.services.document_classifier import aplicar_clasificacion

    aplicar_clasificacion(doc)
    db.add(doc)
    await db.flush()

    await db.commit()
    await db.refresh(doc)

    await logger.ainfo("document_uploaded", doc_id=str(doc.id), filename=filename, contrato_id=str(contrato_id))

    # Optional: link uploaded document to a cuenta de cobro checklist requisito.
    if cuenta_cobro_id is not None and requisito_codigo is not None:
        from app.services import checklist_service

        try:
            await checklist_service.vincular_documento_fuente(
                db=db,
                cuenta_id=cuenta_cobro_id,
                requisito_codigo=requisito_codigo,
                documento_fuente_id=doc.id,
            )
            await db.commit()
        except Exception as exc:
            await logger.awarning(
                "checklist_link_failed",
                doc_id=str(doc.id),
                cuenta_cobro_id=str(cuenta_cobro_id),
                requisito_codigo=requisito_codigo,
                error=str(exc),
            )

    return DocumentUploadResponse(
        id=doc.id,
        nombre=doc.nombre,
        tipo=doc.tipo.value,
        texto_extraido=texto_extraido,
        contrato_id=contrato_id,
        contrato_creado=contrato_creado,
        obligaciones_extraidas=obligaciones_extraidas,
        avisos=avisos,
    )


async def get_documento_download_url(
    db: AsyncSession,
    user_id: uuid.UUID,
    doc_id: uuid.UUID,
    expires_in: int = 3600,
) -> str:
    """Return a presigned download URL for a user-uploaded document."""
    result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.id == doc_id,
            DocumentoFuente.usuario_id == user_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise NotFoundError("Documento", str(doc_id))

    storage = _get_storage(settings.S3_BUCKET_DOCUMENTOS)
    url = await storage.presigned_url(doc.storage_key, expires_in=expires_in)
    return url


async def get_documento_bytes(
    db: AsyncSession,
    user_id: uuid.UUID,
    doc_id: uuid.UUID,
) -> tuple[bytes, str, str]:
    """Stream a user-uploaded document's bytes for direct download.

    Unlike ``get_documento_download_url`` (presigned URL, which does not resolve
    for STORAGE_PROVIDER=local), this reads the bytes through the storage port so
    downloads work identically in local dev and production. Returns
    ``(content, filename, media_type)``.
    """
    import mimetypes

    result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.id == doc_id,
            DocumentoFuente.usuario_id == user_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise NotFoundError("Documento", str(doc_id))

    storage = _get_storage(settings.S3_BUCKET_DOCUMENTOS)
    content = await storage.download(doc.storage_key)
    media_type, _ = mimetypes.guess_type(doc.nombre)
    return content, doc.nombre, media_type or "application/octet-stream"


async def process_document(
    db: AsyncSession,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
) -> DocumentProcessResponse:
    """Re-parse an existing document and update extracted text."""
    result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.id == document_id,
            DocumentoFuente.usuario_id == user_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise NotFoundError("Documento", str(document_id))

    # Download from storage and re-parse
    storage = _get_storage(settings.S3_BUCKET_DOCUMENTOS)
    content = await storage.download(doc.storage_key)
    texto = parse_document(content, doc.nombre)

    doc.texto_extraido = texto
    await db.commit()

    await logger.ainfo("document_processed", doc_id=str(doc.id))

    return DocumentProcessResponse(
        document_id=doc.id,
        texto_extraido=texto,
        metadata=doc.metadata_json,
    )


async def listar_documentos_contrato(
    db: AsyncSession,
    user_id: uuid.UUID,
    contrato_id: uuid.UUID,
) -> list[DocumentoFuenteResponse]:
    """List the CONTRACT-LEVEL documents of a contract (shared across all cuentas).

    Two-tier model: only documents with ``cuenta_cobro_id IS NULL`` are contract-level
    (contract PDF, RUT, cédula, RPC…). Documents that belong to a specific cuenta de
    cobro are excluded here — they are visible through that cuenta's checklist.
    """
    # Verify contrato ownership
    r = await db.execute(
        select(Contrato).where(
            Contrato.id == contrato_id,
            Contrato.usuario_id == user_id,
            Contrato.deleted_at.is_(None),
        )
    )
    if r.scalar_one_or_none() is None:
        raise NotFoundError("Contrato", str(contrato_id))

    result = await db.execute(
        select(DocumentoFuente)
        .where(
            DocumentoFuente.contrato_id == contrato_id,
            DocumentoFuente.usuario_id == user_id,
            DocumentoFuente.cuenta_cobro_id.is_(None),
        )
        .order_by(DocumentoFuente.created_at.desc())
    )
    docs = result.scalars().all()
    return [
        DocumentoFuenteResponse(
            id=d.id,
            nombre=d.nombre,
            tipo=d.tipo,
            contrato_id=d.contrato_id,
            tiene_texto=bool(d.texto_extraido),
            created_at=d.created_at,
        )
        for d in docs
    ]


async def eliminar_documento(
    db: AsyncSession,
    user_id: uuid.UUID,
    doc_id: uuid.UUID,
) -> None:
    """Delete an uploaded document and remove it from storage."""
    result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.id == doc_id,
            DocumentoFuente.usuario_id == user_id,
        )
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise NotFoundError("Documento", str(doc_id))

    # Reset any checklist rows linked to this document back to PENDIENTE. The FK is
    # ON DELETE SET NULL, which would otherwise leave a CARGADO row with no document.
    from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito

    linked = await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.documento_fuente_id == doc_id))
    for fila in linked.scalars().all():
        fila.documento_fuente_id = None
        fila.confianza_deteccion = None
        fila.estado = EstadoRequisito.PENDIENTE

    storage = _get_storage(settings.S3_BUCKET_DOCUMENTOS)
    try:
        await storage.delete(doc.storage_key)
    except Exception:
        await logger.awarning("documento_storage_delete_failed", key=doc.storage_key, doc_id=str(doc_id))

    await db.delete(doc)
    await db.commit()
    await logger.ainfo("documento_eliminado", doc_id=str(doc_id), user_id=str(user_id))


async def verificar_configuracion_contrato(
    db: AsyncSession,
    user_id: uuid.UUID,
    contrato_id: uuid.UUID,
) -> ContratoConfiguracionResponse:
    """Check if a contract has all required documents and configuration to generate cuentas de cobro."""
    # Load contrato with obligaciones
    r = await db.execute(
        select(Contrato)
        .options(selectinload(Contrato.obligaciones))
        .where(
            Contrato.id == contrato_id,
            Contrato.usuario_id == user_id,
            Contrato.deleted_at.is_(None),
        )
    )
    contrato = r.scalar_one_or_none()
    if contrato is None:
        raise NotFoundError("Contrato", str(contrato_id))

    # Load documents scoped to this contract
    docs_result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.contrato_id == contrato_id,
            DocumentoFuente.usuario_id == user_id,
        )
    )
    docs = docs_result.scalars().all()

    # Check custom Plantilla for this user
    plantilla_result = await db.execute(
        select(Plantilla).where(
            Plantilla.tipo == TipoPlantilla.CUENTA_COBRO,
            Plantilla.activa.is_(True),
            (Plantilla.usuario_id == user_id) | (Plantilla.usuario_id.is_(None)),
        )
    )
    plantilla = plantilla_result.scalars().first()

    # Evaluate conditions
    texto_contrato_docs = [d for d in docs if d.tipo == TipoDocumentoFuente.CONTRATO and d.texto_extraido]
    instrucciones_docs = [d for d in docs if d.tipo == TipoDocumentoFuente.INSTRUCCIONES]
    tiene_texto_contrato = len(texto_contrato_docs) > 0
    tiene_instrucciones = len(instrucciones_docs) > 0
    tiene_plantilla = plantilla is not None  # default template always exists; custom is a bonus
    tiene_obligaciones = len(contrato.obligaciones) > 0

    faltantes: list[str] = []
    if not tiene_texto_contrato:
        faltantes.append("Texto del contrato (sube el PDF/Word del contrato como tipo=contrato)")
    if not tiene_instrucciones:
        faltantes.append("Instrucciones/directivas (sube un doc como tipo=instrucciones con indicaciones al agente)")
    if not tiene_obligaciones:
        faltantes.append("Obligaciones contractuales (agrega las obligaciones en POST /contratos/{id}/obligaciones)")

    listo = tiene_texto_contrato and tiene_instrucciones and tiene_obligaciones

    # Build system prompt if we have enough context
    system_prompt: str | None = None
    if listo or (tiene_texto_contrato or tiene_obligaciones):
        texto_contrato = texto_contrato_docs[0].texto_extraido if texto_contrato_docs else "(no disponible)"
        instrucciones_texto = (
            "\n".join(d.texto_extraido for d in instrucciones_docs if d.texto_extraido)
            or "(no se han cargado instrucciones específicas)"
        )
        obligaciones_lista = (
            "\n".join(
                f"{i + 1}. [{ob.tipo.value.upper()}] {ob.descripcion}"
                for i, ob in enumerate(sorted(contrato.obligaciones, key=lambda o: o.orden))
            )
            or "(sin obligaciones registradas)"
        )

        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            numero_contrato=contrato.numero_contrato,
            entidad=contrato.entidad or "—",
            dependencia=contrato.dependencia or "—",
            supervisor=contrato.supervisor_nombre or "—",
            objeto=contrato.objeto,
            fecha_inicio=contrato.fecha_inicio.isoformat(),
            fecha_fin=contrato.fecha_fin.isoformat(),
            valor_total=f"{float(contrato.valor_total):,.2f}",
            valor_mensual=f"{float(contrato.valor_mensual):,.2f}",
            obligaciones=obligaciones_lista,
            texto_contrato=texto_contrato[:4000] if texto_contrato else "(no disponible)",
            instrucciones=instrucciones_texto[:2000],
        )

    docs_response = [
        DocumentoFuenteResponse(
            id=d.id,
            nombre=d.nombre,
            tipo=d.tipo,
            contrato_id=d.contrato_id,
            tiene_texto=bool(d.texto_extraido),
            created_at=d.created_at,
        )
        for d in docs
    ]

    return ContratoConfiguracionResponse(
        contrato_id=contrato_id,
        listo=listo,
        tiene_texto_contrato=tiene_texto_contrato,
        tiene_instrucciones=tiene_instrucciones,
        tiene_plantilla=tiene_plantilla,
        tiene_obligaciones=tiene_obligaciones,
        faltantes=faltantes,
        documentos=docs_response,
        system_prompt=system_prompt,
    )


async def _extraer_obligaciones_escaneado_doc(
    doc: DocumentoFuente,
    contrato_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[list[ObligacionExtraida], list[str]]:
    """Recover obligations from a stored CONTRATO document with no usable text.

    Escalation (cheapest first): local OCR → deterministic extractor; only if the
    OCR text is insufficient does it fall back to the vision model (last resort).
    Downloads the original file once and caches the recovered text on the document
    so future runs can use the fast text path.
    """
    mime = guess_mime_type(doc.nombre)
    if not is_multimodal_supported(mime):
        return [], [
            "El documento del contrato no tiene texto seleccionable y su formato no puede procesarse "
            "(ni por OCR ni por visión). Sube un PDF con texto seleccionable o una imagen legible."
        ]

    ocr_on = settings.EXTRACTION_OCR_ENABLED and ocr_available(settings.EXTRACTION_OCR_ENGINE)
    vision_on = settings.EXTRACTION_MULTIMODAL_FALLBACK_ENABLED
    if not ocr_on and not vision_on:
        return [], [
            "El documento del contrato no tiene texto seleccionable (posible escaneo) y no hay OCR ni "
            "visión disponibles. Instala el motor OCR o habilita EXTRACTION_MULTIMODAL_FALLBACK_ENABLED."
        ]

    try:
        storage = _get_storage(settings.S3_BUCKET_DOCUMENTOS)
        content = await storage.download(doc.storage_key)
    except Exception as exc:
        await logger.awarning("obligaciones_escaneado_download_failed", key=doc.storage_key, error=str(exc))
        return [], [f"No se pudo descargar el documento del almacenamiento para procesarlo ({exc})."]

    # ── Tier 2: local OCR → deterministic extractor (free, fast, no LLM) ──
    if ocr_on:
        try:
            texto_ocr = ocr_extract_text(
                content,
                mime,
                engine=settings.EXTRACTION_OCR_ENGINE,
                lang=settings.EXTRACTION_OCR_LANG,
                max_pages=settings.MULTIMODAL_MAX_PDF_PAGES,
                dpi=settings.MULTIMODAL_RASTER_DPI,
            )
        except Exception as exc:
            texto_ocr = ""
            await logger.awarning("obligaciones_ocr_failed", key=doc.storage_key, error=str(exc))
        if is_text_sufficient(texto_ocr, settings.EXTRACTION_MIN_TEXT_CHARS):
            obligaciones, avisos = await _extraer_obligaciones(texto_ocr, contrato_id, db)
            if obligaciones:
                if not (doc.texto_extraido or "").strip():
                    doc.texto_extraido = texto_ocr
                await logger.ainfo("obligaciones_via_ocr", contrato_id=str(contrato_id), total=len(obligaciones))
                return obligaciones, avisos

    # ── Tier 3: vision model (last resort) ──
    if not vision_on:
        return [], [
            "El OCR no recuperó texto suficiente del documento escaneado y el fallback de visión está "
            "deshabilitado. Habilita EXTRACTION_MULTIMODAL_FALLBACK_ENABLED para procesarlo con visión."
        ]

    result = await _extraer_contrato_multimodal(content, mime)
    if result is None:
        return [], [
            "No se pudo procesar el PDF o la imagen escaneada del contrato con el modelo de visión. "
            "Verifica la configuración (GEMINI_API_KEY y EXTRACTION_MULTIMODAL_FALLBACK_ENABLED)."
        ]

    if result.transcripcion and not (doc.texto_extraido or "").strip():
        doc.texto_extraido = result.transcripcion

    ob_items = _obligacion_items_to_extraidas(result.obligaciones)
    return await _persist_obligaciones(ob_items, contrato_id, db, [])


async def extraer_obligaciones_documento(
    contrato_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[list[ObligacionExtraida], list[str]]:
    """Extrae obligaciones del documento tipo CONTRATO vinculado al contrato.

    Busca el primer DocumentoFuente con tipo=CONTRATO vinculado al contrato y
    verifica que pertenezca al usuario. Si el documento tiene texto suficiente,
    usa el pipeline determinístico/LLM; si es un escaneado sin texto seleccionable,
    cae al modelo de visión (cuando el fallback está habilitado). Persiste las
    obligaciones en DB.

    Returns (obligaciones, avisos).
    Raises NotFoundError si no hay documento de contrato vinculado.
    """
    result = await db.execute(
        select(DocumentoFuente)
        .join(Contrato, DocumentoFuente.contrato_id == Contrato.id)
        .where(
            DocumentoFuente.contrato_id == contrato_id,
            DocumentoFuente.tipo == TipoDocumentoFuente.CONTRATO,
            DocumentoFuente.texto_extraido.is_not(None),
            Contrato.usuario_id == user_id,
            Contrato.deleted_at.is_(None),
        )
        .limit(1)
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise NotFoundError(
            "DocumentoFuente",
            f"No se encontró un documento tipo 'contrato' con texto extraído para el contrato {contrato_id}. "
            "Primero sube el PDF del contrato con POST /documentos/upload?tipo=contrato.",
        )

    texto = doc.texto_extraido or ""
    if is_text_sufficient(texto, settings.EXTRACTION_MIN_TEXT_CHARS):
        obligaciones, avisos = await _extraer_obligaciones(texto, contrato_id, db)
    elif settings.EXTRACTION_OCR_ENABLED or settings.EXTRACTION_MULTIMODAL_FALLBACK_ENABLED:
        await logger.ainfo("obligaciones_escaneado_fallback", contrato_id=str(contrato_id), key=doc.storage_key)
        obligaciones, avisos = await _extraer_obligaciones_escaneado_doc(doc, contrato_id, db)
    else:
        obligaciones, avisos = await _extraer_obligaciones(texto, contrato_id, db)
        if not obligaciones:
            avisos.append(
                "El documento del contrato no tiene texto seleccionable (posible escaneo) y no hay OCR ni "
                "visión habilitados. Instala el motor OCR o habilita EXTRACTION_MULTIMODAL_FALLBACK_ENABLED."
            )

    await db.commit()
    await logger.ainfo(
        "obligaciones_extraidas_endpoint",
        contrato_id=str(contrato_id),
        total=len(obligaciones),
    )
    return obligaciones, avisos


async def actualizar_categoria(
    db: AsyncSession,
    doc_id: uuid.UUID,
    user_id: uuid.UUID,
    categoria: CategoriaDocumento,
) -> DocumentoFuente:
    """Override the category of an uploaded DocumentoFuente (ownership check enforced)."""
    res = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.id == doc_id,
            DocumentoFuente.usuario_id == user_id,
        )
    )
    doc = res.scalar_one_or_none()
    if doc is None:
        raise NotFoundError("DocumentoFuente", str(doc_id))

    doc.categoria = categoria
    doc.categoria_confianza = None
    doc.categoria_override = True
    await db.flush()
    return doc
