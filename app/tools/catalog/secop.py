"""Tool wrappers over `secop_service` — SECOP II contract lookup and import.

Thin wrappers only: ownership/validation rules live in `secop_service` itself,
this module just maps `ToolContext` to the service's `(db, ..., usuario_id)`
calling convention and declares the input/output schemas for the registry.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.secop import SecopContratoResponse, SecopImportResult
from app.services import secop_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class BuscarSecopPorCedulaInput(BaseModel):
    cedula: str = Field(description="Contractor's cédula (national ID), 5 to 15 digits, digits only.")
    refresh: bool = Field(
        default=False,
        description="Force a fresh fetch from the SECOP Socrata API instead of using the local cache.",
    )


class BuscarSecopPorCedulaOutput(BaseModel):
    contratos: list[SecopContratoResponse] = Field(
        description="Prestación de servicios contracts found in SECOP II for this cédula."
    )


@tool(
    name="buscar_secop_por_cedula",
    description=(
        "Search SECOP II (Colombia's public procurement portal) for prestación de servicios "
        "contracts belonging to a given contractor cédula. Read-only: this only queries and "
        "caches SECOP data locally, it never creates or modifies Contrato rows. "
        "Args: cedula (contractor's national ID, 5-15 digits); "
        "refresh (bool, default False — set True to force re-fetching from SECOP instead of "
        "using the local cache when it is still considered fresh)."
    ),
    input_model=BuscarSecopPorCedulaInput,
    output_model=BuscarSecopPorCedulaOutput,
    tags=("read",),
)
async def buscar_secop_por_cedula(ctx: ToolContext, params: BuscarSecopPorCedulaInput) -> BuscarSecopPorCedulaOutput:
    contratos = await secop_service.buscar_contratos_cedula(ctx.db, params.cedula, refresh=params.refresh)
    return BuscarSecopPorCedulaOutput(contratos=contratos)


class ImportarContratoSecopInput(BaseModel):
    documento_proveedor: str = Field(description="Contractor's document number (cédula), 5 to 15 digits.")
    confirmar: bool = Field(
        default=True,
        description=(
            "When True (default), persists the matched SECOP contracts into the user's "
            "contratos table. When False, returns a preview without writing anything to the DB."
        ),
    )


@tool(
    name="importar_contrato_secop",
    description=(
        "Import the authenticated user's SECOP II contracts (matched by documento_proveedor) "
        "into their contratos table, skipping duplicates and invalid/incomplete rows and "
        "updating existing contracts whose value changed. Args: documento_proveedor (contractor's "
        "document number, 5-15 digits); confirmar (bool, default True — set False to preview the "
        "import without persisting anything)."
    ),
    input_model=ImportarContratoSecopInput,
    output_model=SecopImportResult,
    tags=("write",),
)
async def importar_contrato_secop(ctx: ToolContext, params: ImportarContratoSecopInput) -> SecopImportResult:
    return await secop_service.importar_contratos_secop(
        ctx.db,
        documento_proveedor=params.documento_proveedor,
        usuario_id=ctx.usuario_id,
        confirmar=params.confirmar,
    )
