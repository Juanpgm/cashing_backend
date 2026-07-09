"""Tool wrapper over `cuenta_cobro_service.listar_cuentas_cobro` — discover the user's
cuentas de cobro (monthly invoices).

Ownership is enforced by the underlying service via a join on `Contrato.usuario_id` —
a user only ever sees cuentas that hang off one of their own contratos, and an
`contrato_id` belonging to another user simply yields an empty list rather than
leaking a NotFound/Forbidden distinction.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.cuenta_cobro import EstadoCuentaCobro
from app.services import cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.registry import tool

# Keeps the result small enough to always fit in the tool-result truncation budget
# (`agent_chat_service._MAX_TOOL_RESULT_CHARS`) even for a very active user.
_MAX_RESULTS = 50


class ListarCuentasCobroInput(BaseModel):
    contrato_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional. Restrict the list to one contrato_id (obtained from listar_contratos). "
            "Omit to list cuentas de cobro across all of the user's contratos."
        ),
    )


class CuentaCobroResumen(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    mes: int
    anio: int
    estado: EstadoCuentaCobro
    valor: Decimal


class ListarCuentasCobroOutput(BaseModel):
    cuentas: list[CuentaCobroResumen]


@tool(
    name="listar_cuentas_cobro",
    description=(
        "Lista las cuentas de cobro (facturas mensuales) del usuario autenticado, de la más "
        "reciente a la más antigua (máximo 50 resultados). Usa esta herramienta para verificar "
        "si ya existe una cuenta de cobro para un mes/año/contrato antes de intentar crear una "
        "nueva (crear_cuenta_cobro falla si ya existe), o para obtener el id de una cuenta que "
        "necesitan otras herramientas (por ejemplo radicar_cuenta o resumen_checklist) cuando el "
        "usuario no te dio ese UUID explícitamente. Args: contrato_id (opcional; UUID obtenido "
        "con listar_contratos — si se omite, lista las cuentas de todos los contratos del "
        "usuario). Solo lectura, segura de llamar repetidamente."
    ),
    input_model=ListarCuentasCobroInput,
    output_model=ListarCuentasCobroOutput,
    tags=("read",),
)
async def listar_cuentas_cobro(ctx: ToolContext, params: ListarCuentasCobroInput) -> ListarCuentasCobroOutput:
    cuentas = await cuenta_cobro_service.listar_cuentas_cobro(ctx.db, ctx.usuario_id, params.contrato_id)
    resumenes = [
        CuentaCobroResumen(
            id=c.id,
            contrato_id=c.contrato_id,
            mes=c.mes,
            anio=c.anio,
            estado=c.estado,
            valor=c.valor,
        )
        for c in cuentas[:_MAX_RESULTS]
    ]
    return ListarCuentasCobroOutput(cuentas=resumenes)
