"""Documentos API — upload and process source documents."""

from __future__ import annotations

from fastapi import APIRouter, Depends, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.core.file_validation import (
    validate_file_extension,
    validate_file_size,
    validate_mime_type,
)
from app.models.documento_fuente import TipoDocumentoFuente
from app.schemas.agent import DocumentProcessRequest, DocumentProcessResponse, DocumentUploadResponse
from app.services import document_service

router = APIRouter(prefix="/documentos", tags=["documentos"])


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
async def upload_document(
    file: UploadFile,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    tipo: TipoDocumentoFuente = TipoDocumentoFuente.CONTRATO,
) -> DocumentUploadResponse:
    """Upload a source document (contract, instructions, template)."""
    if not file.filename:
        raise ValidationError("Filename is required")

    if not validate_file_extension(file.filename):
        raise ValidationError(f"File type not allowed: {file.filename}")

    content = await file.read()

    if not validate_file_size(len(content)):
        raise ValidationError("File exceeds maximum size of 10MB")

    if file.content_type and not validate_mime_type(content, file.content_type):
        raise ValidationError(f"Invalid MIME type: {file.content_type}")

    return await document_service.upload_document(
        db=db,
        user_id=user.id,
        filename=file.filename,
        content=content,
        content_type=file.content_type or "application/octet-stream",
        tipo=tipo,
    )


@router.post("/process", response_model=DocumentProcessResponse)
async def process_document(
    body: DocumentProcessRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DocumentProcessResponse:
    """Re-process an existing document to extract/update text."""
    return await document_service.process_document(
        db=db,
        user_id=user.id,
        document_id=body.document_id,
    )
