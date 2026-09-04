"""Tool wrapper over `requisito_inference_service.inferir_requisitos_estructurados`
(billing-resilience-templates, slice #7) and over `requisito_cuenta_service.definir_set`
— the post-creation checklist-build gate.

Read-only, non-persisted structured extraction — richer than the flat preview
the existing `POST /cuentas-cobro/{id}/requisitos/inferir` HTTP endpoint
returns (name/category/`solo_primera_cuenta`/autogen-support fields per item).
Applying a reviewed subset still goes through the existing
`POST /cuentas-cobro/{id}/requisitos` (`DefinirRequisitosBody`), unchanged.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.schemas.checklist import ChecklistResponse, ChecklistResumen, RequisitoChecklistItem
from app.schemas.requisito_cuenta import (
    RequisitoCuentaItem,
    RequisitosEstructuradosPreview,
    RequisitosModo,
)
from app.services import (
    checklist_service,
    cuenta_cobro_service,
    requisito_cuenta_service,
    requisito_inference_service,
)
from app.tools.context import ToolContext
from app.tools.registry import tool


class InferirRequisitosEstructuradosInput(BaseModel):
    texto: str = Field(description="Raw text (pasted or extracted) of a requirement document.")


@tool(
    name="inferir_requisitos_estructurados",
    description=(
        "Infer a STRUCTURED requisitos checklist from a contracting-entity requirement "
        "document's text: each item includes name (etiqueta), category, whether it only "
        "applies to the first cuenta (solo_primera_cuenta), and whether it plausibly "
        "supports autogeneration (permite_autogen) — not a flat unstructured list. "
        "Read-only, non-persisted preview. Args: texto (raw requirement-document text)."
    ),
    input_model=InferirRequisitosEstructuradosInput,
    output_model=RequisitosEstructuradosPreview,
    tags=("read",),
)
async def inferir_requisitos_estructurados(
    ctx: ToolContext, params: InferirRequisitosEstructuradosInput
) -> RequisitosEstructuradosPreview:
    return await requisito_inference_service.inferir_requisitos_estructurados(ctx.db, params.texto)


class DefinirRequisitosChecklistInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to build the document checklist for.")
    modo: RequisitosModo = Field(
        default="estandar",
        description=(
            "estandar = usa solo el catálogo estándar de requisitos (default correcto cuando el "
            "usuario NO te dio un documento de requisitos de la entidad contratante); augment = "
            "catálogo estándar + los 'requisitos' custom que agregues; reemplazar = solo los "
            "'requisitos' custom (más EVIDENCIAS y cualquier estándar al que un custom mapee)."
        ),
    )
    requisitos: list[RequisitoCuentaItem] = Field(
        default_factory=list,
        description=(
            "Requisitos custom a aplicar (usados solo si modo es 'augment' o 'reemplazar'). "
            "Normalmente vienen de un preview previo de inferir_requisitos_estructurados; "
            "dejar vacío junto con modo='estandar' es la opción por defecto correcta."
        ),
    )


class DefinirRequisitosChecklistOutput(BaseModel):
    cuenta_cobro_id: uuid.UUID
    modo: RequisitosModo
    requisitos_custom: int = Field(description="How many custom requisitos are active for this cuenta.")
    items: list[RequisitoChecklistItem]
    resumen: ChecklistResumen
    aviso: str | None = Field(
        default=None,
        description=(
            "Set only when the call was a NO-OP because the checklist was already defined and "
            "has progress (documents linked) — the set was NOT replaced. None on a normal apply."
        ),
    )


async def _tiene_progreso(db: AsyncSession, cuenta_id: uuid.UUID) -> bool:
    """Whether ANY checklist row for this cuenta has moved past pendiente —
    i.e. a document/SECOP link, manual fulfilment, or no_aplica already happened.
    """
    res = await db.execute(
        select(DocumentoCuentaCobro.id)
        .where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.estado != EstadoRequisito.PENDIENTE,
        )
        .limit(1)
    )
    return res.scalar_one_or_none() is not None


@tool(
    name="definir_requisitos_checklist",
    description=(
        "Define el modo de construcción del checklist de documentos de una cuenta de cobro y lo "
        "materializa. OBLIGATORIO llamar esta herramienta INMEDIATAMENTE después de "
        "crear_cuenta_cobro y ANTES de cualquier otra herramienta sobre esa cuenta "
        "(resumen_checklist, importar_documento con cuenta_cobro_id, generar_informe_actividades, "
        "generar_informe_supervision, preparar_radicacion, radicar_cuenta) — una cuenta recién "
        "creada tiene requisitos_modo=NULL y esas herramientas fallan o devuelven un checklist "
        "vacío hasta que esto se llama. Si el usuario NO te dio un documento con los requisitos "
        "de la entidad contratante, usa modo='estandar' (el default) sin argumento 'requisitos' — "
        "esa es la opción correcta en la mayoría de los casos. Args: cuenta_id (UUID de la cuenta "
        "de cobro; debe pertenecer al usuario autenticado); modo ('estandar'/'augment'/"
        "'reemplazar', default 'estandar'); requisitos (lista opcional de requisitos custom, solo "
        "relevante para 'augment'/'reemplazar'). LLAMALA UNA SOLA VEZ por cuenta, inmediatamente "
        "después de crearla. NUNCA la vuelvas a llamar sobre una cuenta que ya tiene documentos "
        "vinculados en su checklist — reemplaza el set completo y arrastra en cascada el borrado "
        "de esos vínculos. Si igual la llamás de nuevo en ese caso, la herramienta detecta el "
        "riesgo y no hace nada (devuelve el checklist actual sin tocarlo)."
    ),
    input_model=DefinirRequisitosChecklistInput,
    output_model=DefinirRequisitosChecklistOutput,
    tags=("write",),
)
async def definir_requisitos_checklist(
    ctx: ToolContext, params: DefinirRequisitosChecklistInput
) -> DefinirRequisitosChecklistOutput:
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, params.cuenta_id)

    if cuenta.requisitos_modo is not None and await _tiene_progreso(ctx.db, cuenta.id):
        # F4: RequisitoCuenta bulk-delete (requisito_cuenta_service.definir_set)
        # CASCADEs DocumentoCuentaCobro via ondelete=CASCADE — including CARGADO
        # rows. A re-call here would silently wipe already-linked documents, so
        # this is a defensive no-op instead: return the CURRENT checklist unchanged.
        payload = await checklist_service.construir_checklist_completo(ctx.db, cuenta)
        checklist = ChecklistResponse(**payload)
        set_actual = await requisito_cuenta_service.obtener_set(ctx.db, ctx.usuario_id, params.cuenta_id)
        return DefinirRequisitosChecklistOutput(
            cuenta_cobro_id=checklist.cuenta_cobro_id,
            modo=cuenta.requisitos_modo,
            requisitos_custom=len(set_actual.requisitos),
            items=checklist.items,
            resumen=checklist.resumen,
            aviso=(
                "El checklist de esta cuenta ya estaba definido y tiene documentos vinculados — "
                "no se modificó para evitar perder lo ya cargado. No hace falta volver a llamar "
                "esta herramienta sobre esta cuenta."
            ),
        )

    resultado = await requisito_cuenta_service.definir_set(
        ctx.db, ctx.usuario_id, params.cuenta_id, params.modo, params.requisitos
    )
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(ctx.db, ctx.usuario_id, params.cuenta_id)
    payload = await checklist_service.construir_checklist_completo(ctx.db, cuenta)
    checklist = ChecklistResponse(**payload)
    return DefinirRequisitosChecklistOutput(
        cuenta_cobro_id=checklist.cuenta_cobro_id,
        modo=resultado.modo or params.modo,
        requisitos_custom=len(resultado.requisitos),
        items=checklist.items,
        resumen=checklist.resumen,
    )
