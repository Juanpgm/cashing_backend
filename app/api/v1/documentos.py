"""Documentos API — upload and process source documents per contract."""

import uuid

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.core.exceptions import ChecklistLinkError, ValidationError
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

# Upper bound on files per `/upload-batch` request.
MAX_BATCH_SIZE = 20


def _resolver_tipo(tipo: TipoDocumentoFuente | None, cuenta_cobro_id: uuid.UUID | None) -> TipoDocumentoFuente:
    """Resolve the effective document type when the caller omitted ``tipo``.

    ``tipo`` used to default to ``contrato`` unconditionally, which made an OMISSION a
    contract-level write: the document was stored as the contract's text and read back
    as such by ``verificar_configuracion_contrato`` (which loads documents by
    ``contrato_id`` regardless of cuenta scope).

    The default is now scope-aware instead of removing it outright — making ``tipo``
    required would 422 every existing caller. A request carrying ``cuenta_cobro_id`` is
    a checklist attachment, so it falls back to the neutral ``otros``; the plain
    contract-upload flow keeps ``contrato``. Callers that send ``tipo`` explicitly (all
    current first-party clients do) are unaffected.
    """
    if tipo is not None:
        return tipo
    return TipoDocumentoFuente.OTROS if cuenta_cobro_id is not None else TipoDocumentoFuente.CONTRATO


# B3 hardening: ``document_service.upload_document`` now requires an EXPLICIT
# ``requisito_codigo == "CONTRATO"`` before treating an upload as the contract
# itself (replace rule + obligation extraction) — ``tipo == CONTRATO`` alone is
# no longer sufficient, and this router does NOT default/synthesize it: the
# frontend's `buildContratoUploadParams` (cashing-frontend/lib/documentos-api.ts)
# already sends `requisito_codigo=CONTRATO` explicitly whenever `tipo=contrato`
# for its three contract-upload call sites. Backend-side defaulting here would
# reintroduce the exact bug this hardening fixes: any direct/agent caller that
# passes `tipo=CONTRATO` without meaning "this is the contract" (e.g. a
# certificado de experiencia uploaded via the same dropzone) would be silently
# treated as the contract again.


@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
@limiter.limit("10/minute")
async def upload_document(
    request: Request,
    file: UploadFile,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    tipo: TipoDocumentoFuente | None = Query(
        None,
        description=(
            "Tipo de documento: "
            "**contrato** = texto del contrato firmado (PDF/Word), "
            "**instrucciones** = directivas del usuario para el agente IA, "
            "**plantilla** = plantilla HTML personalizada para el PDF de cuenta de cobro, "
            "**otros** = tipo neutro para adjuntos sin tipo declarado (EVIDENCIAS y "
            "requisitos personalizados). "
            "Si se omite: **otros** cuando se envía `cuenta_cobro_id`, **contrato** en caso contrario."
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

    if len(content) == 0:
        raise ValidationError(f"File '{file.filename}' is empty.")

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
        tipo=_resolver_tipo(tipo, cuenta_cobro_id),
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
@limiter.limit("10/minute")
async def upload_documents_batch(
    request: Request,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    files: list[UploadFile] = File(..., description="One or more files to upload (PDF, DOCX, JPG, PNG, etc.)"),
    tipo: TipoDocumentoFuente | None = Query(
        None,
        description=(
            "Document type applied to ALL files in the batch. "
            "If omitted: `otros` when `cuenta_cobro_id` is sent, `contrato` otherwise."
        ),
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
    """Sube varios documentos en una sola petición (multipart/form-data).

    Acepta cualquier mezcla de PDF, DOCX, TXT, JPG, PNG de hasta 10 MB cada uno.
    Máximo 20 archivos por petición.

    ### Contrato de la respuesta
    - **201** — *todos* los archivos se guardaron **y** se vincularon. La lista trae
      un elemento por archivo, en el mismo orden de entrada.
    - **422** — la validación previa rechazó el lote (nombre, extensión, tamaño,
      MIME): no se guardó ningún archivo.
    - **502** (`code: CHECKLIST_LINK_FAILED`) — al menos un archivo se guardó pero no
      pudo vincularse al checklist. El resto del lote sí se procesó y guardó; volver
      a subir los archivos nombrados en `detail` repara la vinculación.
    - **429** — se superó el límite de 10 peticiones por minuto.

    Cada elemento de `results` incluye `nombre_original` (el nombre tal como lo envió
    el cliente) para poder emparejar resultados con archivos sin replicar el saneado
    de nombres del backend.
    """
    if len(files) > MAX_BATCH_SIZE:
        raise ValidationError(f"Batch exceeds maximum of {MAX_BATCH_SIZE} files.")

    tipo_efectivo = _resolver_tipo(tipo, cuenta_cobro_id)

    # Pass 1 — validate EVERY file (filename, extension, size, MIME) before persisting
    # any of them. Reading+validating file N used to happen only after file N-1 had
    # already been committed to the DB, so a batch of [valid.pdf, bad.exe] left
    # valid.pdf persisted and the whole 422 response discarded — the client had no way
    # to know a document was actually written, and a retry duplicated it.
    payloads: list[tuple[UploadFile, bytes]] = []
    for file in files:
        if not file.filename:
            raise ValidationError("All files must have a filename.")

        if not validate_file_extension(file.filename):
            raise ValidationError(f"File type not allowed: {file.filename}")

        content = await file.read()

        if len(content) == 0:
            raise ValidationError(f"File '{file.filename}' is empty.")

        if not validate_file_size(len(content)):
            raise ValidationError(f"File '{file.filename}' exceeds maximum size of 10 MB.")

        if file.content_type and not validate_mime_type(content, file.content_type):
            raise ValidationError(f"Invalid MIME type for '{file.filename}': {file.content_type}")

        payloads.append((file, content))

    # Pass 2 — all files passed validation, now persist, in input order.
    #
    # A 2xx from this endpoint means EVERY file was persisted AND linked. A
    # `ChecklistLinkError` (document written, checklist link failed) used to be
    # swallowed here by the blanket `except Exception` and downgraded to an
    # `[Error] ...` string on the LAST result's avisos, with the response still 201 —
    # the frontend showed green while the requisito stayed Pendiente, which is exactly
    # what raising it from `upload_document` was meant to stop.
    #
    # The loop still FINISHES on a link failure (the remaining files deserve to be
    # persisted), and the 502 is raised afterwards naming the affected files.
    results: list[DocumentUploadResponse] = []
    link_failed: list[str] = []
    errors: list[str] = []
    # Read the id ONCE: rolling back a failed file expires every ORM instance in the
    # session, `user` (loaded by the auth dependency) included, and a later `user.id`
    # would then trigger a lazy refresh from sync context (MissingGreenlet).
    user_id = user.id

    for file, content in payloads:
        try:
            result = await document_service.upload_document(
                db=db,
                user_id=user_id,
                filename=file.filename,  # type: ignore[arg-type]
                content=content,
                content_type=file.content_type or "application/octet-stream",
                tipo=tipo_efectivo,
                contrato_id=contrato_id,
                cuenta_cobro_id=cuenta_cobro_id,
                requisito_codigo=requisito_codigo,
            )
            results.append(result)
        except ChecklistLinkError:
            # The document IS persisted and committed; only the link is missing.
            link_failed.append(file.filename or "")
            # Drop whatever the failed link left pending so the next file starts from
            # a clean session.
            await db.rollback()
        except Exception as exc:
            errors.append(f"{file.filename}: {exc}")
            await db.rollback()

    if link_failed:
        raise ChecklistLinkError(
            requisito_codigo or "",
            "no se pudo completar la vinculación con el checklist",
            archivos=link_failed,
        )

    if errors and not results:
        raise ValidationError(f"Ningún archivo pudo subirse: {'; '.join(errors)}")

    if errors:
        # Never return 2xx for a partially failed batch: the caller cannot tell which
        # of its files made it, and a blind retry duplicates the ones that did.
        raise ValidationError(f"Algunos archivos no pudieron subirse: {'; '.join(errors)}")

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
