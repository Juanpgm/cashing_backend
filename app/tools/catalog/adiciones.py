"""Tool wrappers over `adicion_contrato_service` — contract Adición event log.

`registrar_adicion_contrato` records a new Adición/prórroga/otrosí event (never
overwrites a prior one — every call inserts a new row). `listar_adiciones_contrato`
is read-only and returns the full ordered event history for a contract.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.adicion_contrato import TipoAdicion
from app.schemas.adicion import AdicionOut
from app.services import adicion_contrato_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class RegistrarAdicionContratoInput(BaseModel):
    contrato_id: uuid.UUID = Field(description="Contrato id the Adición event belongs to.")
    tipo: TipoAdicion = Field(description="Event type: adicion (value increase), prorroga (term extension), otrosi.")
    numero: int = Field(ge=1, description="Sequential adición number for the contract.")
    fecha_evento: date = Field(description="Effective date of the event.")
    rpc_nuevo: str | None = Field(default=None, description="New RPC identifier issued by this event, if any.")
    cdp_nuevo: str | None = Field(default=None, description="New CDP identifier issued by this event, if any.")
    valor_adicion: Decimal | None = Field(default=None, description="Addition value in COP, if applicable/known.")
    nueva_fecha_fin: date | None = Field(default=None, description="New contract end date, if this is a prórroga.")
    descripcion: str = Field(default="", description="Free-text description of the event.")


class ListarAdicionesContratoInput(BaseModel):
    contrato_id: uuid.UUID = Field(description="Contrato id to list Adición events for.")


class AdicionesContratoResponse(BaseModel):
    contrato_id: uuid.UUID
    adiciones: list[AdicionOut]


@tool(
    name="registrar_adicion_contrato",
    description=(
        "Record a new contract Adición/prórroga/otrosí event: new RPC/CDP identifiers "
        "(when issued), valor_adicion, and prórroga extension (when granted). Never "
        "overwrites a prior event — every call inserts a new row, preserving the full "
        "history. Args: contrato_id (UUID), tipo (adicion|prorroga|otrosi), numero (int, "
        ">=1), fecha_evento (date), rpc_nuevo (optional str), cdp_nuevo (optional str), "
        "valor_adicion (optional decimal, COP), nueva_fecha_fin (optional date), "
        "descripcion (optional str)."
    ),
    input_model=RegistrarAdicionContratoInput,
    output_model=AdicionOut,
    tags=("write",),
)
async def registrar_adicion_contrato(ctx: ToolContext, params: RegistrarAdicionContratoInput) -> AdicionOut:
    evento = await adicion_contrato_service.registrar_adicion(
        ctx.db,
        ctx.usuario_id,
        params.contrato_id,
        tipo=params.tipo,
        numero=params.numero,
        fecha_evento=params.fecha_evento,
        rpc_nuevo=params.rpc_nuevo,
        cdp_nuevo=params.cdp_nuevo,
        valor_adicion=params.valor_adicion,
        nueva_fecha_fin=params.nueva_fecha_fin,
        descripcion=params.descripcion,
    )
    return AdicionOut.model_validate(evento)


@tool(
    name="listar_adiciones_contrato",
    description=(
        "Return the full ordered Adición/prórroga/otrosí event history for a contract "
        "(fecha_evento ascending). Read-only. Args: contrato_id (UUID of the contrato; "
        "must belong to the authenticated user)."
    ),
    input_model=ListarAdicionesContratoInput,
    output_model=AdicionesContratoResponse,
    tags=("read",),
)
async def listar_adiciones_contrato(
    ctx: ToolContext, params: ListarAdicionesContratoInput
) -> AdicionesContratoResponse:
    eventos = await adicion_contrato_service.listar_adiciones(ctx.db, ctx.usuario_id, params.contrato_id)
    return AdicionesContratoResponse(
        contrato_id=params.contrato_id,
        adiciones=[AdicionOut.model_validate(e) for e in eventos],
    )
