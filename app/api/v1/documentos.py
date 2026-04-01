"""Documentos API — upload and process source documents per contract."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, UploadFile
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
from app.schemas.documento_fuente import DocumentoFuenteResponse
from app.services import document_service

router = APIRouter(prefix="/documentos", tags=["documentos"])


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
async def upload_document(
    file: UploadFile,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    tipo: TipoDocumentoFuente = Query(
        TipoDocumentoFuente.CONTRATO,
        description=(
            "Tipo de documento: "
            "**contrato** = texto del contrato firmado (PDF/Word), "
            "**instrucciones** = directivas del usuario para el agente IA, "
            "**plantilla** = plantilla HTML personalizada para el PDF de cuenta de cobro."
        ),
    ),
    contrato_id: uuid.UUID | None = Query(
        None,
        description=(
            "UUID del contrato al que pertenece este documento. "
            "**Opcional para tipo=contrato**: si no se proporciona, el sistema extrae "
            "automáticamente los datos del contrato desde el PDF y crea el registro. "
            "**Requerido** para `instrucciones` y `plantilla`. "
            "Obtenlo en `GET /contratos/`."
        ),
        example="00000000-0000-0000-0000-000000000000",
    ),
) -> DocumentUploadResponse:
    """Sube un documento fuente, opcionalmente vinculado a un contrato.

    ### Tipos de documento
    | tipo | Descripción | Uso por el agente |
    |------|-------------|-------------------|
    | `contrato` | PDF/Word del contrato firmado | Contexto normativo y obligaciones |
    | `instrucciones` | Archivo .txt/.docx con directivas | Guía de redacción para actividades |
    | `plantilla` | HTML de la plantilla del PDF | Formato visual de la cuenta de cobro |

    ### Auto-creación de contrato
    Cuando `tipo=contrato` y **no** se proporciona `contrato_id`, el sistema:
    1. Extrae el texto del documento PDF/Word
    2. Usa IA para identificar los datos del contrato (número, objeto, valor, fechas, entidad, etc.)
    3. Crea automáticamente el registro del contrato en la base de datos
    4. Extrae las obligaciones contractuales del contratista
    5. Devuelve el contrato creado (`contrato_creado`) y las obligaciones extraídas

    ### Flujo recomendado
    **Opción A — Contrato ya existe:**
    1. Importar contrato: `POST /secop/importar` o `POST /contratos/`
    2. Subir texto del contrato: `POST /documentos/upload?tipo=contrato&contrato_id=...`
    3. Subir instrucciones: `POST /documentos/upload?tipo=instrucciones&contrato_id=...`

    **Opción B — Auto-crear contrato desde PDF:**
    1. Subir PDF del contrato: `POST /documentos/upload?tipo=contrato` *(sin contrato_id)*
    2. El contrato se crea automáticamente con datos extraídos por IA
    3. Subir instrucciones: `POST /documentos/upload?tipo=instrucciones&contrato_id=...`

    4. Verificar: `GET /contratos/{id}/configuracion`
    """
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
        contrato_id=contrato_id,
    )


@router.post("/process", response_model=DocumentProcessResponse)
async def process_document(
    body: DocumentProcessRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DocumentProcessResponse:
    """Re-procesa un documento existente para extraer o actualizar su texto.

    Útil si la extracción de texto falló en la subida inicial o si el documento fue actualizado.
    """
    return await document_service.process_document(
        db=db,
        user_id=user.id,
        document_id=body.document_id,
    )


@router.get("/contrato/{contrato_id}", response_model=list[DocumentoFuenteResponse])
async def listar_documentos_contrato(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[DocumentoFuenteResponse]:
    """Lista todos los documentos cargados para un contrato específico.

    Muestra qué tipos de documentos ya están configurados y si tienen texto extraído.
    Use `GET /contratos/{id}/configuracion` para ver el estado de completitud.
    """
    return await document_service.listar_documentos_contrato(db, user.id, contrato_id)
