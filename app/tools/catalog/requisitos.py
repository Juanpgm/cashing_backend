"""Tool wrapper over `requisito_inference_service.inferir_requisitos_estructurados`
(billing-resilience-templates, slice #7).

Read-only, non-persisted structured extraction — richer than the flat preview
the existing `POST /cuentas-cobro/{id}/requisitos/inferir` HTTP endpoint
returns (name/category/`solo_primera_cuenta`/autogen-support fields per item).
Applying a reviewed subset still goes through the existing
`POST /cuentas-cobro/{id}/requisitos` (`DefinirRequisitosBody`), unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.requisito_cuenta import RequisitosEstructuradosPreview
from app.services import requisito_inference_service
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
