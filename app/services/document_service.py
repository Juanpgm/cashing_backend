"""Document processing service — upload, parse, and process documents."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.agent.tools.document_parser import parse_document
from app.core.config import settings
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.schemas.agent import DocumentProcessResponse, DocumentUploadResponse

logger = structlog.get_logger("services.document")


async def upload_document(
    db: AsyncSession,
    user_id: uuid.UUID,
    filename: str,
    content: bytes,
    content_type: str,
    tipo: TipoDocumentoFuente = TipoDocumentoFuente.CONTRATO,
) -> DocumentUploadResponse:
    """Upload a document to storage and create a DB record."""
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
        storage_key=storage_key,
        nombre=filename,
        tipo=tipo,
        texto_extraido=texto_extraido,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    await logger.ainfo("document_uploaded", doc_id=str(doc.id), filename=filename)

    return DocumentUploadResponse(
        id=doc.id,
        nombre=doc.nombre,
        tipo=doc.tipo.value,
        texto_extraido=texto_extraido,
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
        from app.core.exceptions import NotFoundError
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
