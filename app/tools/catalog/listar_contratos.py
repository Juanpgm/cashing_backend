"""Tool wrapper over `contrato_service.listar_contratos` — discover the user's contracts.

Exposes a read-only, LLM-friendly summary of the authenticated user's own contratos so
the agent can discover a `contrato_id` on its own (rather than asking the user for a
UUID they'll never have handy) before calling any tool that requires one, e.g.
`crear_cuenta_cobro` or `listar_cuentas_cobro`.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.services import contrato_service
from app.tools.context import ToolContext
from app.tools.registry import tool

# The full `objeto` text can run to several paragraphs — pointless bulk for a tool
# result the LLM only needs to recognize/disambiguate a contract by.
_MAX_OBJETO_CHARS = 200
# Keep the result small enough to always fit in the tool-result truncation budget
# (`agent_chat_service._MAX_TOOL_RESULT_CHARS`) even for a very active user.
_MAX_RESULTS = 20


class ListarContratosInput(BaseModel):
    """No required input — the list is always scoped to the authenticated user."""


class ContratoResumen(BaseModel):
    id: uuid.UUID
    numero_contrato: str
    entidad: str | None = Field(default=None, description="Entidad contratante, si está registrada.")
    objeto: str = Field(description="Objeto del contrato (truncado a 200 caracteres).")
    valor_mensual: Decimal
    fecha_inicio: date
    fecha_fin: date


class ListarContratosOutput(BaseModel):
    contratos: list[ContratoResumen]


@tool(
    name="listar_contratos",
    description=(
        "Lista los contratos del usuario autenticado, del más reciente al más antiguo "
        "(máximo 20 resultados). USA ESTA HERRAMIENTA PRIMERO para descubrir el contrato_id "
        "de un contrato — por ejemplo antes de crear una cuenta de cobro — cuando el usuario "
        "no te haya dado explícitamente ese UUID (nunca lo inventes ni lo pidas al usuario si "
        "puedes buscarlo aquí). No recibe argumentos. Devuelve por cada contrato: id, "
        "numero_contrato, entidad, objeto (truncado), valor_mensual, fecha_inicio y fecha_fin. "
        "Solo lectura, segura de llamar repetidamente."
    ),
    input_model=ListarContratosInput,
    output_model=ListarContratosOutput,
    tags=("read",),
)
async def listar_contratos(ctx: ToolContext, params: ListarContratosInput) -> ListarContratosOutput:
    contratos = await contrato_service.listar_contratos(ctx.db, ctx.usuario_id)
    resumenes = [
        ContratoResumen(
            id=c.id,
            numero_contrato=c.numero_contrato,
            entidad=c.entidad,
            objeto=c.objeto[:_MAX_OBJETO_CHARS],
            valor_mensual=c.valor_mensual,
            fecha_inicio=c.fecha_inicio,
            fecha_fin=c.fecha_fin,
        )
        for c in contratos[:_MAX_RESULTS]
    ]
    return ListarContratosOutput(contratos=resumenes)
