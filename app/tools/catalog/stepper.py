"""Tool wrapper over `stepper_state_service` — the 7-step radicación readiness
aggregate the frontend stepper reads (`GET /cuentas-cobro/{id}/stepper-state`).

Read-only, no credits, no side effects — `obtener_stepper_state` already
resolves ownership internally, so this wrapper needs no extra gate.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.stepper_state import StepperStateResponse
from app.services import stepper_state_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class ObtenerEstadoRadicacionInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to inspect radicación readiness for.")


@tool(
    name="obtener_estado_radicacion",
    description=(
        "Get the aggregate 7-step radicación readiness for a cuenta de cobro (contrato, cuota, "
        "checklist, evidencias, formato, justificaciones, paquete): per-step complete/blocking "
        "state with an error code when blocking, plus current_step/furthest_completed_step. "
        "Read-only, never mutates anything, never charges credits — the same data the frontend "
        "stepper UI reads. A cuenta whose checklist mode hasn't been resolved yet "
        "(requisitos_modo unset) simply reports step 3 as incomplete/blocking with code "
        "CHECKLIST_INCOMPLETE instead of failing; call definir_requisitos_checklist first. Args: "
        "cuenta_id (UUID of the cuenta de cobro; must belong to the authenticated user)."
    ),
    input_model=ObtenerEstadoRadicacionInput,
    output_model=StepperStateResponse,
    tags=("read",),
)
async def obtener_estado_radicacion(ctx: ToolContext, params: ObtenerEstadoRadicacionInput) -> StepperStateResponse:
    return await stepper_state_service.obtener_stepper_state(ctx.db, ctx.usuario_id, params.cuenta_id)
