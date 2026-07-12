"""Tool wrappers over `secop_service` — SECOP II contract lookup and import.

Thin wrappers only: ownership/validation rules live in `secop_service` itself,
this module just maps `ToolContext` to the service's `(db, ..., usuario_id)`
calling convention and declares the input/output schemas for the registry.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.adapters.secop_scraper import get_secop_scraper_gated
from app.schemas.secop import (
    ScraperFallbackResult,
    SecopConfiguracionResponse,
    SecopContratoResponse,
    SecopEstadoDatasetsResponse,
    SecopImportResult,
)
from app.services import secop_scraper_service, secop_service
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


class VerificarConfiguracionSecopInput(BaseModel):
    """No required input — reports this deployment's own SECOP configuration."""


@tool(
    name="verificar_configuracion_secop",
    description=(
        "Check whether the SECOP II Socrata integration is properly configured "
        "(SECOP_APP_TOKEN presence), for diagnostics/support. Read-only, no network call. "
        "Returns status ('ok' or 'degraded'), token_configured, and a human-readable warning "
        "when the token is missing (Socrata throttles unauthenticated requests hard). "
        "No arguments."
    ),
    input_model=VerificarConfiguracionSecopInput,
    output_model=SecopConfiguracionResponse,
    tags=("read",),
)
async def verificar_configuracion_secop(
    ctx: ToolContext, params: VerificarConfiguracionSecopInput
) -> SecopConfiguracionResponse:
    return await secop_service.verificar_configuracion_secop()


class ObtenerEstadoDatasetsSecopInput(BaseModel):
    cedula: str = Field(description="Contractor's cédula (national ID), 5 to 15 digits, digits only.")


@tool(
    name="obtener_estado_datasets_secop",
    description=(
        "Report the schema-verification status of the 3 additional SECOP II datasets wired in "
        "this change (Adiciones, Modificaciones a Procesos, Ubicaciones ejecución) for a "
        "contractor's cédula, plus any accumulated `datasets_con_error`. Read-only, offline: "
        "reflects the outcome of the most recent sincronizar_documentos_secop run(s) recorded on "
        "cached rows, does NOT itself query Socrata. Useful for diagnosing why Adición/ubicación "
        "data may be missing for a given contractor. Args: cedula (contractor's national ID, "
        "5-15 digits)."
    ),
    input_model=ObtenerEstadoDatasetsSecopInput,
    output_model=SecopEstadoDatasetsResponse,
    tags=("read",),
)
async def obtener_estado_datasets_secop(
    ctx: ToolContext, params: ObtenerEstadoDatasetsSecopInput
) -> SecopEstadoDatasetsResponse:
    return await secop_service.obtener_estado_datasets_secop(ctx.db, params.cedula)


class ExplorarDocumentosSecopAgenticoInput(BaseModel):
    numero_contrato: str = Field(
        description="Número/referencia de contrato SECOP (formato CO1.PCCNTR.xxxxxxx) para el que se buscarán "
        "documentos adicionales en la plataforma SECOP II (fuera de los datasets abiertos de Socrata)."
    )


@tool(
    name="explorar_documentos_secop_agentico",
    description=(
        "Manually trigger the SECOP II scraper fallback to look for platform-only documents "
        "(pliegos, anexos, contrato firmado) for one contract, when the open-data (Socrata) "
        "datasets don't carry them. Write tool: it consumes a per-user hourly quota "
        "(SECOP_SCRAPER_HOURLY_LIMIT) before invoking the scraper. Gated behind "
        "SECOP_SCRAPER_ENABLED (default off) — with the flag off this is a safe no-op. "
        "Fail-soft: captcha or scraper-unavailable outcomes come back as a normal result with "
        "estado='captcha_required'/'unavailable', never as an error; only quota exhaustion "
        "raises. Does NOT persist documents (metadata only in this version). "
        "Args: numero_contrato (contract reference, e.g. CO1.PCCNTR.xxxxxxx)."
    ),
    input_model=ExplorarDocumentosSecopAgenticoInput,
    output_model=ScraperFallbackResult,
    tags=("write",),
)
async def explorar_documentos_secop_agentico(
    ctx: ToolContext, params: ExplorarDocumentosSecopAgenticoInput
) -> ScraperFallbackResult:
    scraper = get_secop_scraper_gated()
    return await secop_scraper_service.explorar_documentos_agentico(
        ctx.db, scraper, ctx.usuario_id, params.numero_contrato
    )
