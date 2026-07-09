"""Tool wrappers over `checklist_autogen_service` — generate + link informe DOCX documents.

Mirrors `app/api/v1/checklist.py::generar_requisito`'s exact orchestration for the
two fixed autogenerable requisitos (INFORME_ACTIVIDADES, INFORME_SUPERVISION): gate
on the checklist being defined, ensure the checklist rows exist, generate + link the
document, then return that one requisito's fresh checklist item.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.core.exceptions import ValidationError
from app.schemas.checklist import RequisitoChecklistItem
from app.services import checklist_autogen_service, checklist_service, cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class GenerarInformeInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to generate the informe for.")


async def _generar_y_devolver_item(
    ctx: ToolContext, cuenta_id: uuid.UUID, requisito_codigo: str
) -> RequisitoChecklistItem:
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, cuenta_id)
    if cuenta.requisitos_modo is None:
        raise ValidationError(
            "Definí primero los requisitos del checklist de esta cuenta de cobro antes de generar documentos."
        )
    await checklist_service.asegurar_checklist(ctx.db, cuenta)
    await checklist_autogen_service.generar_y_vincular(ctx.db, ctx.usuario_id, cuenta_id, requisito_codigo)

    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, cuenta_id)
    payload = await checklist_service.construir_checklist_completo(ctx.db, cuenta, auto_vincular=False)
    item = next(
        (i for i in payload["items"] if i["requisito"]["codigo"] == requisito_codigo),
        None,
    )
    if item is None:
        raise ValidationError(f"Requisito {requisito_codigo} not found in checklist.")
    return RequisitoChecklistItem(**item)


@tool(
    name="generar_informe_actividades",
    description=(
        "Generate the 'informe de actividades' DOCX from the cuenta's already-registered "
        "activities and link it into the checklist as cargado for requisito INFORME_ACTIVIDADES. "
        "No file upload needed — the document is produced entirely from data already in the "
        "system. Fails with ValidationError if the checklist is not yet defined for this cuenta "
        "or if it has no activities. Args: cuenta_id (UUID of the cuenta de cobro; must belong to "
        "the authenticated user)."
    ),
    input_model=GenerarInformeInput,
    output_model=RequisitoChecklistItem,
    tags=("write",),
)
async def generar_informe_actividades(ctx: ToolContext, params: GenerarInformeInput) -> RequisitoChecklistItem:
    return await _generar_y_devolver_item(ctx, params.cuenta_id, "INFORME_ACTIVIDADES")


@tool(
    name="generar_informe_supervision",
    description=(
        "Generate the 'informe de supervisión' DOCX from the cuenta's already-registered "
        "activities and link it into the checklist as cargado for requisito INFORME_SUPERVISION. "
        "No file upload needed — the document is produced entirely from data already in the "
        "system. Fails with ValidationError if the checklist is not yet defined for this cuenta "
        "or if it has no activities. Args: cuenta_id (UUID of the cuenta de cobro; must belong to "
        "the authenticated user)."
    ),
    input_model=GenerarInformeInput,
    output_model=RequisitoChecklistItem,
    tags=("write",),
)
async def generar_informe_supervision(ctx: ToolContext, params: GenerarInformeInput) -> RequisitoChecklistItem:
    return await _generar_y_devolver_item(ctx, params.cuenta_id, "INFORME_SUPERVISION")
