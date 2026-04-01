"""Document processing service — upload, parse, and process documents."""

from __future__ import annotations

import re
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.agent.tools.document_parser import parse_document
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.plantilla import Plantilla, TipoPlantilla
from app.schemas.agent import (
    ContratoExtraido,
    DocumentProcessResponse,
    DocumentUploadResponse,
    LLMMessage,
    ObligacionExtraida,
)
from app.schemas.documento_fuente import ContratoConfiguracionResponse, DocumentoFuenteResponse

logger = structlog.get_logger("services.document")

# Lenient: accepts accented/unaccented OBLIGACION, optional markdown bold,
# leading numbering ("1. ", "1) "), bullets ("- ", "* "), and whitespace.
_OBLIGACION_RE = re.compile(
    r"^(?:\d+[.)\-]\s*)?(?:[\-\*]\s*)?\*{0,2}OBLIGACI[OÓ]N\*{0,2}\s*\|\s*(general|espec[ií]fica)\s*\|\s*(.+)$",
    re.IGNORECASE,
)
# Regex for pipe-delimited CAMPO lines from contract metadata extraction
_CAMPO_RE = re.compile(r"^\*{0,2}CAMPO\*{0,2}\s*\|\s*(\w+)\s*\|\s*(.+)$", re.IGNORECASE)
# Valid field names for contract metadata extraction
_CAMPO_VALID_FIELDS = {
    "numero_contrato", "objeto", "valor_total", "valor_mensual",
    "fecha_inicio", "fecha_fin", "supervisor_nombre", "entidad",
    "dependencia", "documento_proveedor",
}
# Max chars per LLM call for obligation extraction.
# ~3-4 chars per token; 8000 chars ≈ 2500 tokens + prompt ≈ 4000 total.
# Groq free tier has 12K TPM so this keeps requests well under limit.
_MAX_CHUNK_CHARS = 8_000
# Overlap between chunks to avoid cutting mid-clause
_CHUNK_OVERLAP = 500
# Keywords that signal the specific-obligations section (tier 1 = preferred).
# If tier-1 keywords are found we ONLY use those sections so the LLM never
# sees the "obligaciones generales" text and can't confuse them.
_OBLIGACION_SECTION_KW_TIER1 = [
    "OBLIGACIONES ESPECIFICAS",
    "OBLIGACIONES ESPECÍFICAS",
]
# Broader fallback (tier 2) — used only when tier-1 finds nothing.
_OBLIGACION_SECTION_KW_TIER2 = [
    "OBLIGACIONES DEL CONTRATISTA",
    "CLAUSULA DE OBLIGACIONES",
    "CLÁUSULA DE OBLIGACIONES",
    "OBLIGACIONES Y RESPONSABILIDADES",
    "OBJETO DEL CONTRATO",
    "ALCANCE DEL TRABAJO",
]


def _extract_obligation_sections(texto: str) -> list[str]:
    """Extract ALL obligation-rich sections from the contract text.

    Scans for every occurrence of every section keyword and builds windows around each.
    Overlapping windows are merged. Returns a list of text chunks to process independently,
    falling back to full-text chunking if no keywords are found.

    Uses a two-tier keyword strategy:
      tier-1 = "OBLIGACIONES ESPECÍFICAS" — preferred, uses ONLY these sections.
      tier-2 = broader keywords — used only when tier-1 finds nothing.
    This prevents the LLM from seeing "obligaciones generales" text.
    """
    texto_upper = texto.upper()

    def _find_ranges(keywords: list[str]) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for kw in keywords:
            pos = 0
            while True:
                idx = texto_upper.find(kw, pos)
                if idx == -1:
                    break
                start = max(0, idx - 300)
                end = min(len(texto), idx + _MAX_CHUNK_CHARS)
                ranges.append((start, end))
                pos = idx + len(kw)
        return ranges

    # Try tier-1 first; fall back to tier-2 only if tier-1 finds nothing
    ranges = _find_ranges(_OBLIGACION_SECTION_KW_TIER1)
    if not ranges:
        ranges = _find_ranges(_OBLIGACION_SECTION_KW_TIER2)

    if not ranges:
        # No keywords found — chunk the full text with overlap
        chunks: list[str] = []
        pos = 0
        while pos < len(texto):
            chunks.append(texto[pos : pos + _MAX_CHUNK_CHARS])
            pos += _MAX_CHUNK_CHARS - _CHUNK_OVERLAP
            if pos + _CHUNK_OVERLAP >= len(texto):
                break
        return chunks or [texto[:_MAX_CHUNK_CHARS]]

    # Merge overlapping/adjacent ranges
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))

    # Split any merged chunk that exceeds _MAX_CHUNK_CHARS
    final_chunks: list[str] = []
    for s, e in merged:
        segment = texto[s:e]
        if len(segment) <= _MAX_CHUNK_CHARS:
            final_chunks.append(segment)
        else:
            pos = 0
            while pos < len(segment):
                final_chunks.append(segment[pos : pos + _MAX_CHUNK_CHARS])
                pos += _MAX_CHUNK_CHARS - _CHUNK_OVERLAP
                if pos + _CHUNK_OVERLAP >= len(segment):
                    break

    return final_chunks


def _parse_obligaciones_llm(response: str) -> list[ObligacionExtraida]:
    """Parse pipe-delimited OBLIGACION lines from LLM output.

    Tolerant to: leading/trailing whitespace, markdown bold markers (**OBLIGACION**),
    extra spaces around pipes, mixed case tipo values, accented characters,
    leading numbering/bullets, and markdown code fences.
    """
    # Strip markdown code fences that LLMs occasionally wrap around output
    cleaned = response.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[^\n]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    result: list[ObligacionExtraida] = []
    orden = 0
    for line in cleaned.splitlines():
        m = _OBLIGACION_RE.match(line.strip())
        if m:
            tipo_raw = m.group(1).lower().strip()
            # Normalize accented "específica" → "especifica"
            tipo_raw = tipo_raw.replace("í", "i")
            descripcion = m.group(2).strip().rstrip(".")
            # Only keep specific obligations (activities the contractor must perform)
            if tipo_raw != "especifica":
                continue
            if descripcion and len(descripcion) > 5:
                result.append(ObligacionExtraida(descripcion=descripcion, tipo=tipo_raw, orden=orden))
                orden += 1
    return result


async def _obtener_obligaciones_existentes(
    contrato_id: uuid.UUID,
    db: AsyncSession,
) -> list[ObligacionExtraida]:
    """Load obligations already stored in DB for a contract."""
    result = await db.execute(
        select(Obligacion)
        .where(Obligacion.contrato_id == contrato_id)
        .order_by(Obligacion.orden)
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

    all_raw: list[ObligacionExtraida] = []
    seen_norm: set[str] = set()
    llm_errors = 0

    for i, chunk in enumerate(chunks):
        messages = [
            LLMMessage(role="system", content=OBLIGACIONES_SYSTEM),
            LLMMessage(role="user", content=OBLIGACIONES_USER.format(texto_contrato=chunk)),
        ]
        try:
            resp = await llm.complete(messages, temperature=0.0, max_tokens=4096)
        except Exception as exc:
            llm_errors += 1
            await logger.awarning(
                "obligaciones_llm_chunk_failed",
                contrato_id=str(contrato_id),
                chunk=i,
                error=str(exc),
            )
            continue

        chunk_obs = _parse_obligaciones_llm(resp.content)
        if not chunk_obs:
            await logger.awarning(
                "obligaciones_parse_zero",
                contrato_id=str(contrato_id),
                chunk=i,
                raw_response=resp.content[:500],
            )
        for ob in chunk_obs:
            norm = ob.descripcion.lower().strip()
            if norm not in seen_norm:
                seen_norm.add(norm)
                all_raw.append(ob)

        await logger.ainfo(
            "obligaciones_chunk_done",
            contrato_id=str(contrato_id),
            chunk=i,
            found=len(chunk_obs),
            tokens=resp.total_tokens,
        )

    if llm_errors > 0:
        avisos.append(
            f"La extracción de obligaciones falló en {llm_errors}/{len(chunks)} fragmentos. "
            "Verifica la configuración del modelo LLM (API key, cuota, conectividad)."
        )

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

    # When contrato_id is None (auto-create failed), return extracted obligations
    # for display only — no DB persistence.
    if contrato_id is None:
        return extraidas, avisos

    # Load existing obligations to deduplicate by normalized description
    existing_result = await db.execute(
        select(Obligacion).where(Obligacion.contrato_id == contrato_id)
    )
    existing_obs = existing_result.scalars().all()
    existing_norm = {ob.descripcion.lower().strip(): ob for ob in existing_obs}

    # Determine next orden value
    next_orden = max((ob.orden for ob in existing_obs), default=0) + 1

    nuevas = 0
    for ob in extraidas:
        norm_key = ob.descripcion.lower().strip()
        if norm_key in existing_norm:
            # Update tipo if changed
            existing_ob = existing_norm[norm_key]
            if existing_ob.tipo.value != ob.tipo:
                existing_ob.tipo = TipoObligacion(ob.tipo)
        else:
            db.add(Obligacion(
                contrato_id=contrato_id,
                descripcion=ob.descripcion,
                tipo=TipoObligacion(ob.tipo),
                orden=next_orden,
            ))
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
    # Return ALL extracted obligations (not just newly inserted)
    return extraidas, avisos


def _parse_campos_llm(response: str) -> dict[str, str]:
    """Parse pipe-delimited CAMPO lines from LLM output into a dict."""
    result: dict[str, str] = {}
    for line in response.splitlines():
        m = _CAMPO_RE.match(line.strip())
        if m:
            field_name = m.group(1).lower().strip()
            value = m.group(2).strip()
            if field_name in _CAMPO_VALID_FIELDS and value:
                result[field_name] = value
    return result


async def _extraer_datos_contrato(
    texto_contrato: str,
) -> ContratoExtraido | None:
    """Call LLM to extract contract metadata from text. Returns ContratoExtraido or None.

    Uses LLM_EXTRACTION_MODEL when set (e.g. ollama/qwen2.5:7b for local zero-cost runs).
    """
    from app.adapters.llm import get_llm
    from app.agent.prompts.contrato_extraction import (
        CONTRATO_EXTRACTION_SYSTEM,
        CONTRATO_EXTRACTION_USER,
    )

    extraction_model = settings.LLM_EXTRACTION_MODEL or None
    llm = get_llm(model=extraction_model)

    # Use the first chunk of the text — contract metadata is usually at the beginning
    chunk = texto_contrato[:_MAX_CHUNK_CHARS]

    messages = [
        LLMMessage(role="system", content=CONTRATO_EXTRACTION_SYSTEM),
        LLMMessage(role="user", content=CONTRATO_EXTRACTION_USER.replace("{texto_contrato}", chunk)),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=2048)
    except Exception as exc:
        await logger.awarning("contrato_extraction_llm_failed", error=str(exc))
        return None

    campos = _parse_campos_llm(resp.content)
    await logger.ainfo(
        "contrato_extraction_done",
        campos_found=list(campos.keys()),
        tokens=resp.total_tokens,
    )

    if not campos.get("numero_contrato") and not campos.get("objeto"):
        await logger.awarning("contrato_extraction_insufficient", campos=campos)
        return None

    # Parse numeric values safely
    from datetime import date as date_type
    from decimal import Decimal, InvalidOperation

    def _safe_decimal(val: str | None) -> Decimal:
        if not val:
            return Decimal("0.00")
        # Remove thousands separators (dots or commas used as such)
        cleaned = val.replace(" ", "").replace("$", "")
        # Handle Colombian format: 12.000.000,00 → 12000000.00
        if "," in cleaned and "." in cleaned:
            if cleaned.rindex(",") > cleaned.rindex("."):
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            # Could be decimal separator: 2000000,00
            parts = cleaned.split(",")
            cleaned = cleaned.replace(",", ".") if len(parts) == 2 and len(parts[1]) <= 2 else cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(".", "", cleaned.count(".") - 1) if cleaned.count(".") > 1 else cleaned
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return Decimal("0.00")

    def _safe_date(val: str | None) -> date_type | None:
        if not val:
            return None
        try:
            return date_type.fromisoformat(val.strip())
        except ValueError:
            return None

    return ContratoExtraido(
        numero_contrato=campos.get("numero_contrato", "SIN-NUMERO"),
        objeto=campos.get("objeto", "Objeto pendiente de revisión"),
        valor_total=_safe_decimal(campos.get("valor_total")),
        valor_mensual=_safe_decimal(campos.get("valor_mensual")),
        fecha_inicio=_safe_date(campos.get("fecha_inicio")),
        fecha_fin=_safe_date(campos.get("fecha_fin")),
        supervisor_nombre=campos.get("supervisor_nombre"),
        entidad=campos.get("entidad"),
        dependencia=campos.get("dependencia"),
        documento_proveedor=campos.get("documento_proveedor"),
    )


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
) -> DocumentUploadResponse:
    """Upload a document to storage and create a DB record.

    When ``tipo=contrato`` and ``contrato_id`` is not provided, the service
    will use the LLM to extract contract metadata from the document text and
    automatically create a ``Contrato`` record linked to the uploaded document.
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

    # Deduplicate: if same filename+tipo+contrato already exists, reuse it
    dup_conditions = [
        DocumentoFuente.usuario_id == user_id,
        DocumentoFuente.nombre == filename,
        DocumentoFuente.tipo == tipo,
    ]
    if contrato_id is not None:
        dup_conditions.append(DocumentoFuente.contrato_id == contrato_id)
    else:
        dup_conditions.append(DocumentoFuente.contrato_id.is_(None))
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
        if tipo == TipoDocumentoFuente.CONTRATO and effective_contrato_id is not None:
            # First check DB for previously extracted obligations
            obligaciones_extraidas = await _obtener_obligaciones_existentes(
                effective_contrato_id, db
            )
            # If none exist and we have text, try extraction now
            if not obligaciones_extraidas and existing_doc.texto_extraido:
                obligaciones_extraidas, ob_avisos = await _extraer_obligaciones(
                    existing_doc.texto_extraido, effective_contrato_id, db
                )
                avisos.extend(ob_avisos)
                if obligaciones_extraidas:
                    await db.commit()
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

    await logger.ainfo(
        "document_text_extracted",
        filename=filename,
        text_length=len(texto_extraido) if texto_extraido else 0,
        contrato_id=str(contrato_id),
        tipo=tipo if isinstance(tipo, str) else tipo.value,
    )

    # Auto-create contract from document text when no contrato_id is provided
    if tipo == TipoDocumentoFuente.CONTRATO and contrato_id is None and texto_extraido:
        contrato_creado = await _extraer_datos_contrato(texto_extraido)
        if contrato_creado is not None:
            from datetime import date as date_type

            contrato = Contrato(
                usuario_id=user_id,
                numero_contrato=contrato_creado.numero_contrato,
                objeto=contrato_creado.objeto,
                valor_total=float(contrato_creado.valor_total),
                valor_mensual=float(contrato_creado.valor_mensual),
                fecha_inicio=contrato_creado.fecha_inicio or date_type.today(),
                fecha_fin=contrato_creado.fecha_fin or date_type.today(),
                supervisor_nombre=contrato_creado.supervisor_nombre,
                entidad=contrato_creado.entidad,
                dependencia=contrato_creado.dependencia,
                documento_proveedor=contrato_creado.documento_proveedor,
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
        else:
            avisos.append(
                "No se pudo crear el contrato automáticamente desde el documento. "
                "Verifica que el PDF contenga datos del contrato (número, objeto, fechas) "
                "o crea el contrato manualmente con POST /contratos/ y vuelve a subir el documento."
            )

    # Upload to S3-compatible storage — non-blocking on failure so text+obligations
    # are always persisted even when MinIO/R2 is unavailable (e.g. local dev).
    storage_key = f"usuarios/{user_id}/documentos/{uuid.uuid4()}/{filename}"
    try:
        storage = S3StorageAdapter(bucket=settings.S3_BUCKET_DOCUMENTOS)
        await storage.upload(key=storage_key, data=content, content_type=content_type)
    except Exception as exc:
        await logger.awarning(
            "storage_upload_failed",
            filename=filename,
            error=str(exc),
            note="Document text and obligations will still be persisted",
        )

    doc = DocumentoFuente(
        usuario_id=user_id,
        contrato_id=contrato_id,
        storage_key=storage_key,
        nombre=filename,
        tipo=tipo,
        texto_extraido=texto_extraido,
    )
    db.add(doc)
    await db.flush()

    # Auto-extract obligations when uploading a contract document.
    # Runs even when contrato_id is None (auto-create failed) so obligations
    # are still returned for display — they just won't be persisted to DB.
    if tipo == TipoDocumentoFuente.CONTRATO and texto_extraido:
        obligaciones_extraidas, ob_avisos = await _extraer_obligaciones(
            texto_extraido, contrato_id, db
        )
        avisos.extend(ob_avisos)

    await db.commit()
    await db.refresh(doc)

    await logger.ainfo("document_uploaded", doc_id=str(doc.id), filename=filename, contrato_id=str(contrato_id))

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
    storage = S3StorageAdapter(bucket=settings.S3_BUCKET_DOCUMENTOS)
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
    """List all documents associated with a specific contract."""
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
        select(DocumentoFuente).where(
            DocumentoFuente.contrato_id == contrato_id,
            DocumentoFuente.usuario_id == user_id,
        ).order_by(DocumentoFuente.created_at.desc())
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
        obligaciones_lista = "\n".join(
            f"{i + 1}. [{ob.tipo.value.upper()}] {ob.descripcion}"
            for i, ob in enumerate(sorted(contrato.obligaciones, key=lambda o: o.orden))
        ) or "(sin obligaciones registradas)"

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
