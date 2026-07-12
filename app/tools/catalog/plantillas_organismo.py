"""Tool wrappers over `requisito_inference_service` — per-organism template
ingestion (billing-resilience-templates, slice #5).

`ingerir_plantilla_organismo` extracts and persists the structure of an
already-uploaded institutional informe template, keyed to the contract's
organism. `obtener_plantilla_organismo` is read-only and returns the
persisted structure (or none, if never ingested).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.plantilla_organismo import (
    IngerirPlantillaOrganismoResponse,
    ObtenerPlantillaOrganismoResponse,
    PlantillaOrganismoOut,
)
from app.services import requisito_inference_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class IngerirPlantillaOrganismoInput(BaseModel):
    contrato_id: uuid.UUID = Field(description="Contrato id whose entidad identifies the organism.")
    documento_fuente_id: uuid.UUID = Field(description="Already-uploaded DocumentoFuente id holding the template.")


class ObtenerPlantillaOrganismoInput(BaseModel):
    contrato_id: uuid.UUID = Field(description="Contrato id whose entidad identifies the organism.")


@tool(
    name="ingerir_plantilla_organismo",
    description=(
        "Extract and persist the structure (columns, sections, anexo references) of an "
        "institutional informe template already uploaded as a DocumentoFuente, keyed to "
        "the contract's organism (normalized Contrato.entidad). Degrades gracefully: if "
        "extraction fails, nothing is persisted and no error is raised — persistida=false "
        "in the response. Args: contrato_id (UUID), documento_fuente_id (UUID)."
    ),
    input_model=IngerirPlantillaOrganismoInput,
    output_model=IngerirPlantillaOrganismoResponse,
    tags=("write",),
)
async def ingerir_plantilla_organismo(
    ctx: ToolContext, params: IngerirPlantillaOrganismoInput
) -> IngerirPlantillaOrganismoResponse:
    plantilla, avisos = await requisito_inference_service.ingerir_plantilla_organismo(
        ctx.db, ctx.usuario_id, params.contrato_id, params.documento_fuente_id
    )
    if plantilla is None:
        return IngerirPlantillaOrganismoResponse(persistida=False, plantilla=None, avisos=avisos)
    return IngerirPlantillaOrganismoResponse(
        persistida=True, plantilla=PlantillaOrganismoOut.model_validate(plantilla), avisos=avisos
    )


@tool(
    name="obtener_plantilla_organismo",
    description=(
        "Return the persisted template structure for a contract's organism (normalized "
        "Contrato.entidad match), or none if never ingested. Read-only. "
        "Args: contrato_id (UUID; must belong to the authenticated user)."
    ),
    input_model=ObtenerPlantillaOrganismoInput,
    output_model=ObtenerPlantillaOrganismoResponse,
    tags=("read",),
)
async def obtener_plantilla_organismo(
    ctx: ToolContext, params: ObtenerPlantillaOrganismoInput
) -> ObtenerPlantillaOrganismoResponse:
    plantilla = await requisito_inference_service.obtener_plantilla_organismo(
        ctx.db, ctx.usuario_id, params.contrato_id
    )
    if plantilla is None:
        return ObtenerPlantillaOrganismoResponse(encontrada=False, plantilla=None)
    return ObtenerPlantillaOrganismoResponse(encontrada=True, plantilla=PlantillaOrganismoOut.model_validate(plantilla))
