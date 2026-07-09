"""Tool wrappers over `cuenta_cobro_service` — create and submit (radicar) cuentas de cobro.

Ownership and credit checks stay in `cuenta_cobro_service`; these wrappers only
adapt `ToolContext` to the service's `(db, usuario_id, ...)` calling convention.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.core.config import settings
from app.schemas.cuenta_cobro import CuentaCobroCreate, CuentaCobroResponse
from app.services import cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.registry import tool


@tool(
    name="crear_cuenta_cobro",
    description=(
        "Create a new cuenta de cobro (monthly invoice) in BORRADOR state for one of the "
        "authenticated user's contratos, deducting credits from their balance. Fails if the "
        "contrato doesn't belong to the user, if credits are insufficient, or if a cuenta "
        "already exists for that contrato/mes/anio. The checklist is NOT created yet — it is "
        "materialized once the requisitos mode is chosen (a separate step). REQUIRES A REAL "
        "contrato_id UUID — if the user didn't give you one explicitly, call `listar_contratos` "
        "FIRST to obtain it; never invent a UUID or pass a placeholder/description string. "
        "REQUIRES BOTH mes AND anio as separate integers — never omit anio even if only the "
        "month was emphasized in the request. Args: contrato_id (UUID, from listar_contratos), "
        "mes (integer 1-12), anio (integer 2020-2099, e.g. 2026), valor (optional; defaults to "
        "the contrato's valor_mensual when omitted). Example call: {\"contrato_id\": "
        "\"<uuid from listar_contratos>\", \"mes\": 7, \"anio\": 2026}."
    ),
    input_model=CuentaCobroCreate,
    output_model=CuentaCobroResponse,
    tags=("write",),
    consumes_credits=settings.CREDITS_PER_CUENTA_COBRO,
)
async def crear_cuenta_cobro(ctx: ToolContext, params: CuentaCobroCreate) -> CuentaCobroResponse:
    return await cuenta_cobro_service.crear_cuenta_cobro(ctx.db, ctx.usuario_id, params)


class RadicarCuentaInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to submit (radicar).")


@tool(
    name="radicar_cuenta",
    description=(
        "Submit (radicar) a cuenta de cobro, transitioning it from BORRADOR/RECHAZADA to "
        "ENVIADA. Gates the transition on checklist readiness: rebuilds the document checklist "
        "and raises a ValidationError (code=CHECKLIST_INCOMPLETE) naming the pending requisitos "
        "if it isn't complete yet. Args: cuenta_id (UUID of the cuenta de cobro; must belong to "
        "the authenticated user)."
    ),
    input_model=RadicarCuentaInput,
    output_model=CuentaCobroResponse,
    tags=("write",),
)
async def radicar_cuenta(ctx: ToolContext, params: RadicarCuentaInput) -> CuentaCobroResponse:
    return await cuenta_cobro_service.radicar_cuenta(ctx.db, ctx.usuario_id, params.cuenta_id)
