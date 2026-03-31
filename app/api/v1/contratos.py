"""Contratos API — CRUD and obligaciones management."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.contrato import (
    ContratoCreate,
    ContratoListItem,
    ContratoResponse,
    ContratoUpdate,
    ObligacionCreate,
    ObligacionResponse,
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
    """Create a new contract. Optionally include obligaciones in the same request."""
    return await contrato_service.crear_contrato(db, user.id, data)


@router.get("/", response_model=list[ContratoListItem])
async def listar_contratos(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ContratoListItem]:
    """List all contracts for the authenticated user, newest first."""
    return await contrato_service.listar_contratos(db, user.id)


@router.get("/{contrato_id}", response_model=ContratoResponse)
async def obtener_contrato(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoResponse:
    """Get a single contract with its obligaciones."""
    return await contrato_service.obtener_contrato(db, user.id, contrato_id)


@router.patch("/{contrato_id}", response_model=ContratoResponse)
async def actualizar_contrato(
    contrato_id: uuid.UUID,
    data: ContratoUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoResponse:
    """Partially update a contract. Only provided fields are changed."""
    return await contrato_service.actualizar_contrato(db, user.id, contrato_id, data)


@router.delete("/{contrato_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def eliminar_contrato(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Soft-delete a contract.
    Blocked if the contract has cuentas de cobro in enviada, aprobada, or pagada state.
    """
    await contrato_service.eliminar_contrato(db, user.id, contrato_id)


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
    """Add an obligation to a contract."""
    return await contrato_service.agregar_obligacion(db, user.id, contrato_id, data)


@router.get("/{contrato_id}/configuracion", response_model=ContratoConfiguracionResponse)
async def verificar_configuracion(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContratoConfiguracionResponse:
    """Verifica si el contrato tiene toda la documentación necesaria para generar cuentas de cobro.

    Comprueba:
    - **texto_contrato**: PDF/Word del contrato subido con tipo=contrato y texto extraído.
    - **instrucciones**: Documento con directivas del usuario (tipo=instrucciones) para guiar al agente.
    - **plantilla**: Plantilla HTML activa (custom o por defecto del sistema).
    - **obligaciones**: Al menos una obligación contractual registrada.

    Si `listo=true`, el campo `system_prompt` contiene el prompt del agente listo para usar.
    """
    return await document_service.verificar_configuracion_contrato(db, user.id, contrato_id)


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
    """
    Delete an obligation from a contract.
    Blocked if any actividad in a cuenta de cobro references it.
    """
    await contrato_service.eliminar_obligacion(db, user.id, contrato_id, obligacion_id)
