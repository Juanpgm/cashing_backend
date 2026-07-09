"""Tool wrapper — import a chat-attached file as a `DocumentoFuente`.

This is the bridge between the free-form chat attachments (`ToolContext.attachments`,
populated only by `agent_chat_service.chat_with_tools`) and the normal document
pipeline (`document_service.upload_document`) that the `/documentos/upload` endpoint
already uses. It re-runs the exact same validation the HTTP endpoint runs — a file
that reached `ToolContext.attachments` was already validated once at the multipart
boundary (`app/api/v1/agent_chat.py`), but this tool can in principle be reached with
a stale/mismatched attachment reference, so validation is not skipped.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

from app.core.exceptions import NotFoundError, ValidationError
from app.core.file_validation import validate_file_extension, validate_file_size, validate_mime_type
from app.models.documento_fuente import TipoDocumentoFuente
from app.schemas.agent import DocumentUploadResponse
from app.services import document_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class ImportarDocumentoInput(BaseModel):
    filename: str = Field(description="Name of a file the user attached in this chat turn (must match exactly).")
    tipo: Literal["contrato", "instrucciones", "plantilla"] = Field(
        default="contrato",
        description=(
            "contrato = texto del contrato firmado (PDF/Word); instrucciones = directivas del "
            "usuario para el agente; plantilla = plantilla HTML del PDF de cuenta de cobro."
        ),
    )
    contrato_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of an existing Contrato to attach this document to. Omit for tipo=contrato to auto-create.",
    )
    cuenta_cobro_id: uuid.UUID | None = Field(
        default=None,
        description="UUID of a CuentaCobro to link this document to a checklist requisito.",
    )
    requisito_codigo: str | None = Field(
        default=None,
        description="Checklist requisito code this document fulfils (requires cuenta_cobro_id).",
    )


class ImportarDocumentoOutput(BaseModel):
    documento_id: uuid.UUID
    contrato_id: uuid.UUID | None = None
    tipo: str
    resumen: str = Field(description="Short Spanish summary of what happened with this import.")


def _build_resumen(result: DocumentUploadResponse) -> str:
    partes: list[str] = [f"Documento '{result.nombre}' importado correctamente."]

    if result.contrato_creado is not None:
        partes.append(
            f"Se creó automáticamente el contrato {result.contrato_creado.numero_contrato or ''}".strip() + "."
        )

    if result.obligaciones_extraidas:
        partes.append(f"Se extrajeron {len(result.obligaciones_extraidas)} obligaciones contractuales.")

    if result.avisos:
        partes.append("Avisos: " + "; ".join(result.avisos))

    return " ".join(partes)


@tool(
    name="importar_documento",
    description=(
        "Importa un archivo adjuntado por el usuario en el chat (PDF, DOCX, XLSX u otro formato "
        "soportado) al sistema de documentos: lo valida, extrae su texto y, si tipo=contrato y no "
        "se da contrato_id, crea automáticamente el contrato y extrae sus obligaciones. Usa esta "
        "herramienta cuando el usuario adjunte un contrato, instrucciones o plantilla en el chat y "
        "quiera que el agente lo procese. El 'filename' debe coincidir exactamente con el nombre de "
        "un archivo adjuntado en este turno de la conversación — no inventes nombres de archivo."
    ),
    input_model=ImportarDocumentoInput,
    output_model=ImportarDocumentoOutput,
    tags=("write", "chat_only"),
)
async def importar_documento(ctx: ToolContext, params: ImportarDocumentoInput) -> ImportarDocumentoOutput:
    attachment = ctx.attachments.get(params.filename)
    if attachment is None:
        raise NotFoundError("Attachment", params.filename)

    if not validate_file_extension(attachment.filename):
        raise ValidationError(f"File type not allowed: {attachment.filename}")

    if not validate_file_size(len(attachment.data)):
        raise ValidationError("File exceeds maximum size of 10MB")

    if attachment.content_type and not validate_mime_type(attachment.data, attachment.content_type):
        raise ValidationError(f"Invalid MIME type: {attachment.content_type}")

    result = await document_service.upload_document(
        db=ctx.db,
        user_id=ctx.usuario_id,
        filename=attachment.filename,
        content=attachment.data,
        content_type=attachment.content_type or "application/octet-stream",
        tipo=TipoDocumentoFuente(params.tipo),
        contrato_id=params.contrato_id,
        cuenta_cobro_id=params.cuenta_cobro_id,
        requisito_codigo=params.requisito_codigo,
    )

    return ImportarDocumentoOutput(
        documento_id=result.id,
        contrato_id=result.contrato_id,
        tipo=result.tipo,
        resumen=_build_resumen(result),
    )
