"""Pydantic schemas for the evidence packager (LISTO/PENDIENTE state + package result)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel


class ObligacionEstadoOut(BaseModel):
    obligacion_id: uuid.UUID
    descripcion: str
    listo: bool


class EstadoListoPendienteResponse(BaseModel):
    cuenta_cobro_id: uuid.UUID
    obligaciones: list[ObligacionEstadoOut]
    pendientes: int
    listo_para_radicar: bool


class PaqueteGeneradoResponse(BaseModel):
    """Result of generating (and persisting) an evidence package."""

    cuenta_cobro_id: uuid.UUID
    storage_key: str
    filename: str
    size_bytes: int
    pendientes: int
    modo: str


class PaqueteInfoResponse(BaseModel):
    """`GET /paquete` — read-only package metadata + LISTO/PENDIENTE content
    summary (radicacion-stepper, work unit B5). `existe` reflects whether an
    object currently sits at the deterministic storage key; `filename` and
    `storage_key` are always the deterministic values this cuota WOULD use,
    regardless of `existe` (harmless to show ahead of generation)."""

    cuenta_cobro_id: uuid.UUID
    existe: bool
    storage_key: str
    filename: str
    size_bytes: int
    listo_para_radicar: bool
    pendientes: int
    obligaciones: list[ObligacionEstadoOut]
