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
from sqlalchemy import select

from app.core.exceptions import NotFoundError, ValidationError
from app.core.file_validation import validate_file_extension, validate_file_size, validate_mime_type
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_fuente import TipoDocumentoFuente
from app.schemas.agent import DocumentUploadResponse
from app.services import checklist_service, document_service
from app.tools.context import ToolContext
from app.tools.registry import tool

# tipo (str) -> requisito codigo, derived from the same seed `checklist_service`
# bootstraps the catalog table from — NOT hand-duplicated, so a new catalog entry
# can never drift out of sync with what this tool derives. `instrucciones`/
# `plantilla`/`evidencias`-shaped tipos have no `tipo_documento_fuente` in the
# seed and are correctly absent here (they stay unlinked unless the caller
# passes requisito_codigo explicitly).
_TIPO_A_REQUISITO_CODIGO: dict[str, str] = {
    item["tipo_documento_fuente"]: item["codigo"]
    for item in checklist_service._CATALOGO_SEED
    if item.get("tipo_documento_fuente")
}

# Built from `TipoDocumentoFuente` (not the enum type itself) so pydantic emits an
# inline `enum` in the JSON schema instead of a `$ref`/`$defs` indirection — some
# LLM tool-calling adapters choke on unresolved `$ref`s in function parameters.
# Keeping this a `Literal` derived from the enum means a new `TipoDocumentoFuente`
# member is picked up automatically, it can never drift out of sync.
_TIPO_DOCUMENTO_VALUES = tuple(t.value for t in TipoDocumentoFuente)
TipoDocumentoImportable = Literal[_TIPO_DOCUMENTO_VALUES]  # type: ignore[valid-type]

_TIPO_DESCRIPTION = (
    "Tipo de documento a importar — determina a qué requisito del checklist se vincula. "
    "contrato = texto del contrato firmado (PDF/Word); crea el contrato automáticamente si no "
    "se da contrato_id y extrae sus obligaciones. instrucciones = directivas del usuario para el "
    "agente (no vinculado a ningún requisito). plantilla = plantilla HTML del PDF de cuenta de "
    "cobro (no vinculado a ningún requisito). rpc = Registro Presupuestal (requisito RPC). "
    "cdp = Certificado de Disponibilidad Presupuestal (requisito CDP). "
    "seguridad_social = planilla de aportes a seguridad social (requisito SEGURIDAD_SOCIAL). "
    "comprobante_pago_ss = comprobante/CUS del pago de seguridad social (requisito "
    "COMPROBANTE_PAGO_SS). informe_actividades = informe de actividades del contratista "
    "(requisito INFORME_ACTIVIDADES) — normalmente se genera con generar_informe_actividades en "
    "vez de subirse. informe_supervision = informe de supervisión (requisito "
    "INFORME_SUPERVISION) — normalmente se genera con generar_informe_supervision. "
    "ds_consecutivo = consecutivo de pago de la entidad (requisito DS_CONSECUTIVO). "
    "cedula = cédula de ciudadanía del contratista (requisito CEDULA). "
    "rut = Registro Único Tributario (requisito RUT). "
    "ficha_tecnica = ficha técnica del contratista (requisito FICHA_TECNICA). "
    "acta_inicio = acta de inicio del contrato firmada (requisito ACTA_INICIO). "
    "dependientes = certificado/declaración de dependientes económicos (requisito DEPENDIENTES)."
)


class ImportarDocumentoInput(BaseModel):
    filename: str = Field(description="Name of a file the user attached in this chat turn (must match exactly).")
    tipo: TipoDocumentoImportable = Field(default="contrato", description=_TIPO_DESCRIPTION)
    contrato_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "UUID de un Contrato existente. OBLIGATORIO (o dá cuenta_cobro_id para resolverlo) "
            "para tipos A NIVEL DE CONTRATO: contrato, rpc, cdp, cedula, rut, ficha_tecnica, "
            "acta_inicio — estos documentos se guardan compartidos por todas las cuentas del "
            "contrato, nunca atados a una sola cuenta. Omitilo para tipo=contrato si querés que "
            "el contrato se cree automáticamente."
        ),
    )
    cuenta_cobro_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "UUID de una CuentaCobro. Para tipos a nivel de CUENTA (seguridad_social, "
            "comprobante_pago_ss, informe_actividades, informe_supervision, ds_consecutivo, "
            "dependientes) es OBLIGATORIO para que el documento quede vinculado al checklist de "
            "esa cuenta. Para tipos a nivel de CONTRATO (contrato, rpc, cdp, cedula, rut, "
            "ficha_tecnica, acta_inicio) se usa SOLO para resolver el contrato cuando no diste "
            "contrato_id — el documento igual se guarda a nivel de contrato, nunca atado a esta "
            "cuenta en particular."
        ),
    )
    requisito_codigo: str | None = Field(
        default=None,
        description=(
            "Checklist requisito code this document fulfils. Se auto-deriva de 'tipo' cuando se "
            "omite y el tipo tiene un requisito asociado en el catálogo — normalmente no hace "
            "falta pasarlo explícitamente salvo para un requisito custom."
        ),
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


async def _resolver_scope(
    ctx: ToolContext, params: ImportarDocumentoInput
) -> tuple[uuid.UUID | None, uuid.UUID | None, str | None]:
    """Normalise (contrato_id, cuenta_cobro_id, requisito_codigo) from `tipo` before
    calling `upload_document` — see module docstring / F1 for the bug this fixes.

    `upload_document` only scopes a document CONTRACT-level (`cuenta_cobro_id=None`,
    the pool `auto_vincular_documentos_fuente` actually scans for nivel-contrato
    requisitos) when the caller passes a nivel-contrato `requisito_codigo` TOGETHER
    with `cuenta_cobro_id`. Passing `cuenta_cobro_id` alone for a contract-level
    `tipo` (no `requisito_codigo`) used to leave the document cuenta-scoped —
    invisible to auto-linking forever. This resolves that here, once, so
    `document_service.upload_document`'s shared scoping semantics (also used by
    the HTTP upload path) are never touched.

    Ownership of `cuenta_cobro_id` is verified with the SAME never-leak-existence
    join `document_service.upload_document` itself uses for its own cuenta_cobro_id
    branch (never `cuenta_cobro_service._get_cuenta_con_ownership`, which raises a
    distinguishable `ForbiddenError` for "exists but not yours" — that would leak
    to another user that a cuenta_cobro_id they guessed actually exists).
    """
    requisito_codigo = params.requisito_codigo or _TIPO_A_REQUISITO_CODIGO.get(params.tipo)
    contrato_id = params.contrato_id
    cuenta_cobro_id = params.cuenta_cobro_id

    if requisito_codigo and checklist_service.es_nivel_contrato(requisito_codigo):
        if contrato_id is None:
            if cuenta_cobro_id is None:
                raise ValidationError(
                    f"Para importar un documento tipo '{params.tipo}' necesitás pasar contrato_id, "
                    "o cuenta_cobro_id para resolver el contrato al que pertenece."
                )
            cc_lookup = await ctx.db.execute(
                select(CuentaCobro)
                .join(Contrato, Contrato.id == CuentaCobro.contrato_id)
                .where(
                    CuentaCobro.id == cuenta_cobro_id,
                    Contrato.usuario_id == ctx.usuario_id,
                    Contrato.deleted_at.is_(None),
                )
            )
            cuenta = cc_lookup.scalar_one_or_none()
            if cuenta is None:
                raise NotFoundError("CuentaCobro", str(cuenta_cobro_id))
            contrato_id = cuenta.contrato_id
        # Contract-level documents are ALWAYS shared across every cuenta of the
        # contract (cuenta_cobro_id=None) — never scope them to one cuenta, or
        # auto_vincular_documentos_fuente's docs_contrato pool can never see them.
        cuenta_cobro_id = None

    return contrato_id, cuenta_cobro_id, requisito_codigo


@tool(
    name="importar_documento",
    description=(
        "Importa un archivo adjuntado por el usuario en el chat (PDF, DOCX, XLSX u otro formato "
        "soportado) al sistema de documentos: lo valida, extrae su texto y lo vincula "
        "automáticamente al lugar correcto según su 'tipo' — al CONTRATO si es un soporte a nivel "
        "de contrato (contrato, rpc, cdp, cedula, rut, ficha_tecnica, acta_inicio) o al checklist "
        "de la CUENTA de cobro si es a nivel de cuenta (seguridad_social, comprobante_pago_ss, "
        "informe_actividades, informe_supervision, ds_consecutivo, dependientes). Si tipo=contrato "
        "y no se da contrato_id, crea automáticamente el contrato y extrae sus obligaciones. Usa "
        "esta herramienta cuando el usuario adjunte cualquier soporte del contrato o de la cuenta "
        "de cobro en el chat y quiera que el agente lo procese. El 'filename' debe coincidir "
        "exactamente con el nombre de un archivo adjuntado en este turno de la conversación — no "
        "inventes nombres de archivo."
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

    contrato_id, cuenta_cobro_id, requisito_codigo = await _resolver_scope(ctx, params)

    result = await document_service.upload_document(
        db=ctx.db,
        user_id=ctx.usuario_id,
        filename=attachment.filename,
        content=attachment.data,
        content_type=attachment.content_type or "application/octet-stream",
        tipo=TipoDocumentoFuente(params.tipo),
        contrato_id=contrato_id,
        cuenta_cobro_id=cuenta_cobro_id,
        requisito_codigo=requisito_codigo,
    )

    return ImportarDocumentoOutput(
        documento_id=result.id,
        contrato_id=result.contrato_id,
        tipo=result.tipo,
        resumen=_build_resumen(result),
    )
