"""Requisitos por cuenta — infer and define the document checklist of a cuenta de cobro.

Post-creation gate flow: after a cuenta de cobro is created, the user chooses how
to build its checklist — use the standard catalog, or infer custom requirements
from a document issued by the contracting entity (uploaded file or pasted text),
review/edit the inferred list, and apply it.
"""

from __future__ import annotations

import uuid

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
from app.schemas.requisito_cuenta import (
    DefinirRequisitosBody,
    InferirTextoBody,
    RequisitosCuentaSet,
    RequisitosInferidosPreview,
)
from app.services import (
    cuenta_cobro_service,
    requisito_cuenta_service,
    requisito_inference_service,
)

router = APIRouter(
    prefix="/cuentas-cobro/{cuenta_id}/requisitos",
    tags=["requisitos"],
)


@router.post("/inferir", response_model=RequisitosInferidosPreview)
async def inferir_desde_texto(
    cuenta_id: uuid.UUID,
    body: InferirTextoBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RequisitosInferidosPreview:
    """Infiere un checklist de requisitos desde texto pegado.

    Devuelve un preview EDITABLE; no persiste nada. El usuario revisa, ajusta y
    luego aplica con `POST /definir`.
    """
    await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)
    preview = await requisito_inference_service.inferir_requisitos(db, body.texto)
    await db.commit()
    return preview


@router.post("/inferir-archivo", response_model=RequisitosInferidosPreview)
async def inferir_desde_archivo(
    cuenta_id: uuid.UUID,
    file: UploadFile,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RequisitosInferidosPreview:
    """Infiere un checklist de requisitos desde un documento de la entidad
    contratante (pliego, estudios previos, invitación, minuta).

    Extrae el texto (PDF/imagen/Word, con OCR cuando hace falta) y luego infiere
    los requisitos. Devuelve un preview EDITABLE; no persiste nada.
    """
    await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)

    if not file.filename:
        raise ValidationError("Filename is required")
    if not validate_file_extension(file.filename):
        raise ValidationError(f"File type not allowed: {file.filename}")

    content = await file.read()
    if not validate_file_size(len(content)):
        raise ValidationError("File exceeds maximum size of 10MB")
    if file.content_type and not validate_mime_type(content, file.content_type):
        raise ValidationError(f"Invalid MIME type: {file.content_type}")

    preview = await requisito_inference_service.inferir_requisitos_desde_archivo(
        db,
        filename=file.filename,
        content=content,
        content_type=file.content_type,
    )
    await db.commit()
    return preview


@router.get("", response_model=RequisitosCuentaSet)
async def obtener_requisitos(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RequisitosCuentaSet:
    """Devuelve el set de requisitos custom y el modo de checklist de la cuenta."""
    result = await requisito_cuenta_service.obtener_set(db, user.id, cuenta_id)
    await db.commit()
    return result


@router.post("", response_model=RequisitosCuentaSet)
async def definir_requisitos(
    cuenta_id: uuid.UUID,
    body: DefinirRequisitosBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RequisitosCuentaSet:
    """Aplica el checklist de la cuenta: fija el modo (`estandar` / `augment` /
    `reemplazar`), reemplaza el set de requisitos custom y materializa el checklist.
    """
    result = await requisito_cuenta_service.definir_set(db, user.id, cuenta_id, body.modo, body.requisitos)
    await db.commit()
    return result
