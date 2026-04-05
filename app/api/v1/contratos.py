"""Contratos API — CRUD, obligaciones, and agent context."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.agent import ObligacionesExtraerResponse
from app.schemas.contrato import (
    ContratoContextoAgenteResponse,
    ContratoCreate,
    ContratoListItem,
    ContratoResponse,
    ContratoUpdate,
    ObligacionCreate,
    ObligacionResponse,
    PeriodoPendienteResponse,
)
from app.schemas.documento_fuente import ContratoConfiguracionResponse
from app.services import contrato_service, document_service

logger = structlog.get_logger("api.contratos")

router = APIRouter(prefix="/contratos", tags=["contratos"])


@router.post("/", response_model=ContratoResponse, status_code=status.HTTP_201_CREATED)
async def crear_contrato(
    data: ContratoCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoResponse:
    """Crea un nuevo contrato. Se pueden incluir las obligaciones en la misma solicitud."""
    return await contrato_service.crear_contrato(db, user.id, data)


@router.get("/", response_model=list[ContratoListItem])
async def listar_contratos(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ContratoListItem]:
    """Lista todos los contratos activos del usuario autenticado, más recientes primero."""
    return await contrato_service.listar_contratos(db, user.id)


@router.get("/{contrato_id}", response_model=ContratoResponse)
async def obtener_contrato(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoResponse:
    """Obtiene un contrato con todas sus obligaciones."""
    return await contrato_service.obtener_contrato(db, user.id, contrato_id)


@router.patch("/{contrato_id}", response_model=ContratoResponse)
async def actualizar_contrato(
    contrato_id: uuid.UUID,
    data: ContratoUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoResponse:
    """Actualiza parcialmente un contrato. Solo se modifican los campos enviados."""
    return await contrato_service.actualizar_contrato(db, user.id, contrato_id, data)


@router.delete("/{contrato_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def eliminar_contrato(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Elimina (soft-delete) un contrato. Bloqueado si tiene cuentas en estado enviada, aprobada o pagada."""
    await contrato_service.eliminar_contrato(db, user.id, contrato_id)


@router.get("/{contrato_id}/configuracion", response_model=ContratoConfiguracionResponse)
async def verificar_configuracion(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoConfiguracionResponse:
    """Verifica si el contrato tiene toda la documentación necesaria para generar cuentas de cobro.

    Comprueba:
    - **texto_contrato**: PDF/Word del contrato subido con `tipo=contrato` y texto extraído.
    - **instrucciones**: Documento con directivas del usuario (`tipo=instrucciones`) para guiar al agente.
    - **plantilla**: Plantilla HTML activa (custom o por defecto del sistema).
    - **obligaciones**: Al menos una obligación contractual registrada.

    Si `listo=true`, el campo `system_prompt` contiene el prompt del agente listo para usar.
    """
    return await document_service.verificar_configuracion_contrato(db, user.id, contrato_id)


@router.get("/{contrato_id}/contexto-agente", response_model=ContratoContextoAgenteResponse)
async def obtener_contexto_agente(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoContextoAgenteResponse:
    """Devuelve el contexto completo que el agente IA necesita para generar una cuenta de cobro.

    Incluye: datos del contrato, obligaciones, texto extraído del contrato,
    instrucciones del usuario, cuentas de cobro previas y el `system_prompt` ensamblado.

    Use este endpoint para alimentar el agente antes de generar actividades.
    """
    return await contrato_service.obtener_contexto_agente(db, user.id, contrato_id)


@router.get("/{contrato_id}/periodos-pendientes", response_model=list[PeriodoPendienteResponse])
async def listar_periodos_pendientes(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PeriodoPendienteResponse]:
    """Lista todos los meses de la vigencia del contrato indicando cuáles no tienen cuenta de cobro.

    Solo muestra hasta el mes actual. Los períodos con `pendiente=true` son los que
    aún no han sido facturados.
    """
    return await contrato_service.listar_periodos_pendientes(db, user.id, contrato_id)


@router.post("/{contrato_id}/obligaciones/extraer", response_model=ObligacionesExtraerResponse)
async def extraer_obligaciones(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ObligacionesExtraerResponse:
    """Extrae obligaciones contractuales del documento PDF del contrato usando LLM.

    Busca el documento tipo `contrato` vinculado al contrato, lee su texto extraído y
    corre el pipeline de extracción (chunking + LLM + deduplicación).
    Las nuevas obligaciones se persisten en DB; las ya existentes se omiten.

    **Requisito:** El documento del contrato debe haberse subido previamente con
    `POST /documentos/upload?tipo=contrato` y el texto debe haberse extraído correctamente.
    """
    obligaciones, avisos = await document_service.extraer_obligaciones_documento(
        contrato_id, user.id, db
    )
    return ObligacionesExtraerResponse(
        contrato_id=contrato_id,
        obligaciones=obligaciones,
        total=len(obligaciones),
        avisos=avisos,
    )


@router.post(
    "/{contrato_id}/obligaciones",
    response_model=ObligacionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def agregar_obligacion(
    contrato_id: uuid.UUID,
    data: ObligacionCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ObligacionResponse:
    """Agrega una obligación contractual al contrato."""
    return await contrato_service.agregar_obligacion(db, user.id, contrato_id, data)


@router.delete(
    "/{contrato_id}/obligaciones/{obligacion_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def eliminar_obligacion(
    contrato_id: uuid.UUID,
    obligacion_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Elimina una obligación del contrato. Bloqueado si alguna actividad la referencia."""
    await contrato_service.eliminar_obligacion(db, user.id, contrato_id, obligacion_id)
