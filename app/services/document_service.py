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
from app.schemas.agent import DocumentProcessResponse, DocumentUploadResponse, LLMMessage, ObligacionExtraida
from app.schemas.documento_fuente import ContratoConfiguracionResponse, DocumentoFuenteResponse

logger = structlog.get_logger("services.document")

# More lenient: accepts optional leading/trailing spaces and markdown bold markers
_OBLIGACION_RE = re.compile(r"^\*{0,2}OBLIGACION\*{0,2}\s*\|\s*(general|especifica)\s*\|\s*(.+)$", re.IGNORECASE)
# Max chars sent to LLM — enough to cover full contract text for most Colombian PSC contracts
_MAX_TEXTO_CHARS = 20_000
# Keywords that signal the obligations section — used to find the most relevant slice
_OBLIGACION_SECTION_KEYWORDS = [
    "OBLIGACIONES DEL CONTRATISTA",
    "OBLIGACIONES ESPECIFICAS",
    "OBLIGACIONES ESPECÍFICAS",
    "OBLIGACIONES GENERALES",
    "CLAUSULA DE OBLIGACIONES",
    "CLÁUSULA DE OBLIGACIONES",
    "OBLIGACIONES Y RESPONSABILIDADES",
]


def _extract_relevant_slice(texto: str, max_chars: int) -> str:
    """Return the most obligation-rich slice of the contract text.

    Tries to find the obligations section and center the window around it.
    Falls back to the full text truncated if no section found.
    """
    texto_upper = texto.upper()
    best_pos = -1
    for kw in _OBLIGACION_SECTION_KEYWORDS:
        pos = texto_upper.find(kw)
        if pos != -1 and (best_pos == -1 or pos < best_pos):
            best_pos = pos

    if best_pos != -1:
        # Include context before the section header + full obligations text after
        start = max(0, best_pos - 500)
        return texto[start : start + max_chars]
    return texto[:max_chars]


def _parse_obligaciones_llm(response: str) -> list[ObligacionExtraida]:
    """Parse pipe-delimited OBLIGACION lines from LLM output.

    Tolerant to: leading/trailing whitespace, markdown bold markers (**OBLIGACION**),
    extra spaces around pipes, and mixed case tipo values.
    """
    result: list[ObligacionExtraida] = []
    orden = 0
    for line in response.splitlines():
        m = _OBLIGACION_RE.match(line.strip())
        if m:
            tipo_raw = m.group(1).lower().strip()
            descripcion = m.group(2).strip().rstrip(".")
            if descripcion and len(descripcion) > 5:
                result.append(ObligacionExtraida(descripcion=descripcion, tipo=tipo_raw, orden=orden))
                orden += 1
    return result


async def _extraer_obligaciones(
    texto_contrato: str,
    contrato_id: uuid.UUID,
    db: AsyncSession,
) -> list[ObligacionExtraida]:
    """Call LLM to extract obligations and persist them. Returns extracted list."""
    from app.adapters.llm import get_llm
    from app.agent.prompts.obligaciones import OBLIGACIONES_SYSTEM, OBLIGACIONES_USER

    llm = get_llm()  # uses LLM_DEFAULT_MODEL (Gemini) with automatic fallback to OpenAI
    texto_slice = _extract_relevant_slice(texto_contrato, _MAX_TEXTO_CHARS)
    messages = [
        LLMMessage(role="system", content=OBLIGACIONES_SYSTEM),
        LLMMessage(role="user", content=OBLIGACIONES_USER.format(texto_contrato=texto_slice)),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=3000)
    except Exception as exc:
        await logger.awarning("obligaciones_llm_failed", contrato_id=str(contrato_id), error=str(exc))
        return []

    extraidas = _parse_obligaciones_llm(resp.content)
    if not extraidas:
        await logger.awarning(
            "obligaciones_llm_empty",
            contrato_id=str(contrato_id),
            raw=resp.content[:300],
            texto_chars=len(texto_slice),
        )
        return []

    # Load existing obligations to deduplicate by normalized description
    existing_result = await db.execute(
        select(Obligacion).where(Obligacion.contrato_id == contrato_id)
    )
    existing_obs = existing_result.scalars().all()
    existing_norm = {ob.descripcion.lower().strip(): ob for ob in existing_obs}

    # Determine next orden value
    next_orden = max((ob.orden for ob in existing_obs), default=0) + 1

    insertadas: list[ObligacionExtraida] = []
    actualizadas: list[ObligacionExtraida] = []

    for ob in extraidas:
        norm_key = ob.descripcion.lower().strip()
        if norm_key in existing_norm:
            # Update tipo/orden only if changed
            existing_ob = existing_norm[norm_key]
            if existing_ob.tipo.value != ob.tipo:
                existing_ob.tipo = TipoObligacion(ob.tipo)
                actualizadas.append(ob)
        else:
            db.add(Obligacion(
                contrato_id=contrato_id,
                descripcion=ob.descripcion,
                tipo=TipoObligacion(ob.tipo),
                orden=next_orden,
            ))
            insertadas.append(ob)
            next_orden += 1

    await db.flush()
    await logger.ainfo(
        "obligaciones_extraidas",
        contrato_id=str(contrato_id),
        insertadas=len(insertadas),
        actualizadas=len(actualizadas),
        duplicadas_omitidas=len(extraidas) - len(insertadas) - len(actualizadas),
    )
    return insertadas + actualizadas

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
    """Upload a document to storage and create a DB record."""
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
    dup_result = await db.execute(select(DocumentoFuente).where(*dup_conditions).limit(1))
    existing_doc = dup_result.scalar_one_or_none()

    obligaciones_extraidas: list[ObligacionExtraida] = []

    if existing_doc is not None:
        # Document already uploaded — skip S3, just try obligation extraction if still pending
        await logger.ainfo(
            "document_already_exists",
            doc_id=str(existing_doc.id),
            filename=filename,
            contrato_id=str(contrato_id),
        )
        if tipo == TipoDocumentoFuente.CONTRATO and contrato_id is not None and existing_doc.texto_extraido:
            obligaciones_extraidas = await _extraer_obligaciones(
                existing_doc.texto_extraido, contrato_id, db
            )
            if obligaciones_extraidas:
                await db.commit()
        return DocumentUploadResponse(
            id=existing_doc.id,
            nombre=existing_doc.nombre,
            tipo=existing_doc.tipo.value,
            texto_extraido=existing_doc.texto_extraido,
            obligaciones_extraidas=obligaciones_extraidas,
        )

    storage = S3StorageAdapter(bucket=settings.S3_BUCKET_DOCUMENTOS)
    storage_key = f"usuarios/{user_id}/documentos/{uuid.uuid4()}/{filename}"

    await storage.upload(key=storage_key, data=content, content_type=content_type)

    # Try to extract text immediately
    texto_extraido: str | None = None
    try:
        texto_extraido = parse_document(content, filename)
    except (ValueError, Exception) as exc:
        await logger.awarning("text_extraction_failed", filename=filename, error=str(exc))

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

    # Auto-extract obligations when uploading a contract document
    if tipo == TipoDocumentoFuente.CONTRATO and contrato_id is not None and texto_extraido:
        obligaciones_extraidas = await _extraer_obligaciones(texto_extraido, contrato_id, db)

    await db.commit()
    await db.refresh(doc)

    await logger.ainfo("document_uploaded", doc_id=str(doc.id), filename=filename, contrato_id=str(contrato_id))

    return DocumentUploadResponse(
        id=doc.id,
        nombre=doc.nombre,
        tipo=doc.tipo.value,
        texto_extraido=texto_extraido,
        obligaciones_extraidas=obligaciones_extraidas,
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
