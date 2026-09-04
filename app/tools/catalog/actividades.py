"""Tool wrappers over `cuenta_cobro_service` activity-creation functions.

Before this module existed, NO tool created `Actividad` rows at all: the only
write path reachable from chat (`persistir_evidencias`) requires the user to
have connected Gmail/Drive/Calendar first. These three thin wrappers expose the
service functions the REST API already ships
(`app/api/v1/cuentas_cobro.py::agregar_actividades_desde_texto` /
`generar_actividades_agente` / `crear_actividades_desde_obligaciones`),
unblocking `generar_informe_actividades`/`generar_informe_supervision` from a
fresh cuenta de cobro with no Google integration connected.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field

from app.schemas.cuenta_cobro import ActividadesBulkResponse
from app.services import cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.registry import tool

# Tool results are truncated at `agent_chat_service._MAX_TOOL_RESULT_CHARS`
# (12000 chars) — a bulk activity response can carry justificaciones/evidencias
# per activity, so only a short preview of descriptions is surfaced to the LLM.
_MAX_PREVIEW = 10


class ActividadesResumenOutput(BaseModel):
    creadas: int = Field(description="Total number of activities created by this call.")
    saltadas: int = Field(
        default=0,
        description=(
            "How many were skipped because an activity already existed for that obligación on "
            "this cuenta (idempotent re-call) — tell the user when this is non-zero instead of "
            "silently reporting 0 creadas as if nothing happened."
        ),
    )
    actividades: list[str] = Field(
        default_factory=list,
        description=f"Descriptions of the first {_MAX_PREVIEW} created activities (preview, not the full list).",
    )


def _compactar(bulk: ActividadesBulkResponse) -> ActividadesResumenOutput:
    return ActividadesResumenOutput(
        creadas=bulk.creadas,
        saltadas=bulk.saltadas,
        actividades=[a.descripcion for a in bulk.actividades[:_MAX_PREVIEW]],
    )


class CrearActividadesDesdeObligacionesInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to seed activities for.")


@tool(
    name="crear_actividades_desde_obligaciones",
    description=(
        "Crea una actividad por cada obligación registrada del contrato (descripcion = el texto "
        "de la obligación), de forma determinística y SIN usar ningún modelo de IA. Es la opción "
        "PREFERIDA y más barata para poblar actividades cuando el contrato ya tiene obligaciones "
        "registradas — úsala antes de generar_actividades_agente salvo que el usuario pida "
        "explícitamente actividades más elaboradas. Falla con error de validación si el contrato "
        "no tiene obligaciones registradas (usa extraer_obligaciones_contrato primero) o si la "
        "cuenta no está en borrador/rechazada. Args: cuenta_id (UUID de la cuenta de cobro; debe "
        "pertenecer al usuario autenticado)."
    ),
    input_model=CrearActividadesDesdeObligacionesInput,
    output_model=ActividadesResumenOutput,
    tags=("write",),
)
async def crear_actividades_desde_obligaciones(
    ctx: ToolContext, params: CrearActividadesDesdeObligacionesInput
) -> ActividadesResumenOutput:
    bulk = await cuenta_cobro_service.crear_actividades_desde_obligaciones(ctx.db, ctx.usuario_id, params.cuenta_id)
    return _compactar(bulk)


class GenerarActividadesAgenteInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to generate activities for.")
    periodo_inicio: date | None = Field(
        default=None, description="Optional start date bounding the period the agent should justify."
    )
    periodo_fin: date | None = Field(
        default=None, description="Optional end date bounding the period the agent should justify."
    )


@tool(
    name="generar_actividades_agente",
    description=(
        "Usa un modelo de IA para generar y persistir actividades con justificación a partir de "
        "las obligaciones registradas y/o el texto del documento de contrato ya cargado. Requiere "
        "que el contrato tenga obligaciones registradas O un documento de contrato cargado; si "
        "ninguno está disponible, falla con error de validación indicando usar "
        "agregar_actividades_desde_texto o extraer_obligaciones_contrato primero. Más costosa que "
        "crear_actividades_desde_obligaciones — úsala cuando el usuario quiera actividades "
        "redactadas/justificadas, no solo el texto crudo de la obligación. Args: cuenta_id (UUID "
        "de la cuenta de cobro; debe pertenecer al usuario autenticado); periodo_inicio/"
        "periodo_fin (fechas opcionales que acotan el período a justificar)."
    ),
    input_model=GenerarActividadesAgenteInput,
    output_model=ActividadesResumenOutput,
    tags=("write",),
)
async def generar_actividades_agente(
    ctx: ToolContext, params: GenerarActividadesAgenteInput
) -> ActividadesResumenOutput:
    bulk = await cuenta_cobro_service.generar_actividades_agente(
        ctx.db,
        ctx.usuario_id,
        params.cuenta_id,
        periodo_inicio=params.periodo_inicio,
        periodo_fin=params.periodo_fin,
    )
    return _compactar(bulk)


class AgregarActividadesDesdeTextoInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to add activities to.")
    texto: str = Field(
        description=(
            "Texto con una actividad por línea numerada (ej.: '1. Elaboré el informe mensual'). "
            "Cada línea debe empezar con un número seguido de punto, paréntesis o guion."
        )
    )
    fecha_realizacion: date | None = Field(
        default=None, description="Fecha en que se realizaron las actividades (opcional, aplica a todas)."
    )
    vincular_obligaciones: bool = Field(
        default=True,
        description=(
            "Si True (default), vincula cada actividad a una obligación del contrato por "
            "posición (línea 1 → obligación 1, etc.) cuando el contrato tiene obligaciones "
            "registradas. Poné False para dejarlas sin vincular."
        ),
    )


@tool(
    name="agregar_actividades_desde_texto",
    description=(
        "Parsea una lista de texto numerada y crea una actividad por línea. Es el ÚLTIMO RECURSO "
        "manual cuando el contrato no tiene obligaciones registradas y el usuario no quiere usar "
        "generar_actividades_agente — por ejemplo cuando el usuario dicta o pega directamente sus "
        "actividades del período. Cada línea debe empezar con un número seguido de '.', ')' o "
        "'-'; texto sin líneas numeradas falla con error de validación. Args: cuenta_id (UUID de "
        "la cuenta de cobro; debe pertenecer al usuario autenticado); texto (lista numerada); "
        "fecha_realizacion (fecha opcional aplicada a todas las actividades creadas); "
        "vincular_obligaciones (bool, default True — vincula por posición a las obligaciones del "
        "contrato si existen)."
    ),
    input_model=AgregarActividadesDesdeTextoInput,
    output_model=ActividadesResumenOutput,
    tags=("write",),
)
async def agregar_actividades_desde_texto(
    ctx: ToolContext, params: AgregarActividadesDesdeTextoInput
) -> ActividadesResumenOutput:
    bulk = await cuenta_cobro_service.agregar_actividades_desde_texto(
        ctx.db,
        ctx.usuario_id,
        params.cuenta_id,
        texto=params.texto,
        fecha_realizacion=params.fecha_realizacion,
        vincular_obligaciones=params.vincular_obligaciones,
    )
    return _compactar(bulk)
