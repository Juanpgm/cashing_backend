"""Tool wrapper over `document_service.extraer_obligaciones_documento`.

Separate module (not `informes.py`, `listar_contratos.py`, or
`importar_documento.py`): this is neither an autogen-informe wrapper, a
read-only contract listing, nor a chat-attachment import — it re-runs
obligation extraction against a contract document ALREADY persisted as a
`DocumentoFuente`, which is its own concern with its own recovery-path
semantics (see the tool description below). A dedicated module keeps each
catalog file single-purpose, matching `checklist.py`/`requisitos.py`.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.schemas.agent import ObligacionesExtraerResponse
from app.services import document_service
from app.tools.context import ToolContext
from app.tools.registry import tool


class ExtraerObligacionesContratoInput(BaseModel):
    contrato_id: uuid.UUID = Field(description="Contrato id whose obligaciones should be (re-)extracted.")


@tool(
    name="extraer_obligaciones_contrato",
    description=(
        "Re-extrae las obligaciones contractuales del documento tipo 'contrato' ya vinculado a "
        "un contrato (importado previamente con importar_documento tipo=contrato). Es el CAMINO "
        "DE RECUPERACIÓN cuando un contrato no tiene obligaciones registradas — por ejemplo si el "
        "documento se importó sin tipo=contrato, la auto-extracción inicial falló, o se necesita "
        "reintentar tras un error. Persiste las obligaciones nuevas en la base de datos (las que "
        "ya existían se omiten). Falla con NotFoundError si el contrato no tiene un documento "
        "tipo 'contrato' con texto extraído — en ese caso usa primero importar_documento con "
        "tipo=contrato. Args: contrato_id (UUID del contrato; debe pertenecer al usuario "
        "autenticado)."
    ),
    input_model=ExtraerObligacionesContratoInput,
    output_model=ObligacionesExtraerResponse,
    tags=("write",),
)
async def extraer_obligaciones_contrato(
    ctx: ToolContext, params: ExtraerObligacionesContratoInput
) -> ObligacionesExtraerResponse:
    obligaciones, avisos = await document_service.extraer_obligaciones_documento(
        params.contrato_id, ctx.usuario_id, ctx.db
    )
    return ObligacionesExtraerResponse(
        contrato_id=params.contrato_id,
        obligaciones=obligaciones,
        total=len(obligaciones),
        avisos=avisos,
    )
