"""Documentos API — upload and process source documents per contract."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.core.file_validation import (
    validate_file_extension,
    validate_file_size,
    validate_mime_type,
)
from app.core.rate_limit import limiter
from app.models.documento_fuente import TipoDocumentoFuente
from app.schemas.agent import DocumentProcessRequest, DocumentProcessResponse, DocumentUploadResponse
from app.schemas.checklist import CategoriaUpdateBody
from app.schemas.documento_fuente import DocumentoFuenteResponse
from app.services import document_service

router = APIRouter(prefix="/documentos", tags=["documentos"])


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
@limiter.limit("10/minute")
async def upload_document(
    request: Request,
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
    cuenta_cobro_id: uuid.UUID | None = Query(
        None,
        description=(
            "UUID de la cuenta de cobro a la que se asocia este documento como evidencia "
            "de un requisito del checklist. Usar en conjunto con `requisito_codigo`."
        ),
    ),
    requisito_codigo: str | None = Query(
        None,
        description=(
            "Código del requisito del checklist al que se vincula el documento "
            "(ej. `CONTRATO`, `RPC`, `SEGURIDAD_SOCIAL`). Requiere `cuenta_cobro_id`."
        ),
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
        cuenta_cobro_id=cuenta_cobro_id,
        requisito_codigo=requisito_codigo,
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


@router.post("/upload-batch", response_model=list[DocumentUploadResponse], status_code=201)
@limiter.limit("3/minute")
async def upload_documents_batch(
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    files: list[UploadFile] = File(..., description="One or more files to upload (PDF, DOCX, JPG, PNG, etc.)"),
    tipo: TipoDocumentoFuente = Query(
        TipoDocumentoFuente.CONTRATO,
        description="Document type applied to ALL files in the batch.",
    ),
    contrato_id: uuid.UUID | None = Query(
        None,
        description="Optional contract UUID to associate with all uploaded files.",
    ),
    cuenta_cobro_id: uuid.UUID | None = Query(
        None,
        description=(
            "UUID de la cuenta de cobro a la que se asocian estos documentos como evidencia. "
            "Si se usa, requiere `requisito_codigo` y aplica a todos los archivos del lote."
        ),
    ),
    requisito_codigo: str | None = Query(
        None,
        description=("Código del requisito del checklist al que se vinculan los documentos. Aplica al lote completo."),
    ),
) -> list[DocumentUploadResponse]:
    """Upload multiple documents in a single request (multipart/form-data).

    Each file is validated individually; failures raise 422 with the filename.
    Successful uploads are returned as a list in the same order as the input files.

    Accepts any mix of PDF, DOCX, TXT, JPG, PNG files up to 10 MB each.
    Maximum 20 files per request.
    """
    MAX_BATCH_SIZE = 20
    if len(files) > MAX_BATCH_SIZE:
        raise ValidationError(f"Batch exceeds maximum of {MAX_BATCH_SIZE} files.")

    results: list[DocumentUploadResponse] = []
    errors: list[str] = []

    for file in files:
        if not file.filename:
            raise ValidationError("All files must have a filename.")

        if not validate_file_extension(file.filename):
            raise ValidationError(f"File type not allowed: {file.filename}")

        content = await file.read()

        if not validate_file_size(len(content)):
            raise ValidationError(f"File '{file.filename}' exceeds maximum size of 10 MB.")

        if file.content_type and not validate_mime_type(content, file.content_type):
            raise ValidationError(f"Invalid MIME type for '{file.filename}': {file.content_type}")

        try:
            result = await document_service.upload_document(
                db=db,
                user_id=user.id,
                filename=file.filename,
                content=content,
                content_type=file.content_type or "application/octet-stream",
                tipo=tipo,
                contrato_id=contrato_id,
                cuenta_cobro_id=cuenta_cobro_id,
                requisito_codigo=requisito_codigo,
            )
            results.append(result)
        except Exception as exc:
            errors.append(f"{file.filename}: {exc}")

    if errors and not results:
        raise ValidationError(f"All files failed to upload: {'; '.join(errors)}")

    if errors and results:
        results[-1].avisos.extend([f"[Error] {e}" for e in errors])

    return results


@router.delete("/{doc_id}", status_code=204)
async def eliminar_documento(
    doc_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Elimina un documento cargado por el usuario y lo borra del almacenamiento."""
    await document_service.eliminar_documento(db=db, user_id=user.id, doc_id=doc_id)


@router.get("/{doc_id}/descargar")
async def descargar_documento(
    doc_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | int]:
    """Genera una URL de descarga temporal (presigned) para un documento cargado.

    La URL expira en 1 hora. Para STORAGE_PROVIDER=local, retorna la URL del storage local.
    """
    url = await document_service.get_documento_download_url(db=db, user_id=user.id, doc_id=doc_id)
    return {"url": url, "expires_in": 3600}


@router.get("/{doc_id}/archivo", response_class=Response)
async def descargar_archivo_documento(
    doc_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Descarga directa del archivo de un documento subido/generado (stream de bytes).

    A diferencia de `/descargar` (URL presigned), sirve los bytes a través del backend,
    por lo que funciona igual en local (STORAGE_PROVIDER=local) y en producción.
    """
    content, filename, media_type = await document_service.get_documento_bytes(db=db, user_id=user.id, doc_id=doc_id)
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{doc_id}/categoria", response_model=DocumentoFuenteResponse)
async def actualizar_categoria_documento_fuente(
    doc_id: uuid.UUID,
    body: CategoriaUpdateBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DocumentoFuenteResponse:
    """Sobreescribe manualmente la categoría de un documento subido por el usuario.

    Marca `categoria_override=true` para que reclasificaciones automáticas no
    lo sobreescriban en el futuro.
    """
    doc = await document_service.actualizar_categoria(db, doc_id, user.id, body.categoria)
    await db.commit()
    await db.refresh(doc)
    return DocumentoFuenteResponse.model_validate(doc)
