"""Schemas for Contrato and Obligacion."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.models.obligacion import TipoObligacion


class ObligacionCreate(BaseModel):
    descripcion: str = Field(min_length=5, max_length=1000)
    tipo: TipoObligacion
    orden: int = Field(default=0, ge=0)

    model_config = {
        "json_schema_extra": {
            "example": {
                "descripcion": "Elaborar informes mensuales de avance de las actividades desarrolladas en cumplimiento del objeto contractual",
                "tipo": "tecnica",
                "orden": 1,
            }
        }
    }


class ObligacionResponse(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    descripcion: str
    tipo: TipoObligacion
    orden: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ContratoCreate(BaseModel):
    numero_contrato: str = Field(min_length=1, max_length=100)
    objeto: str = Field(min_length=10, max_length=2000)
    valor_total: Decimal = Field(gt=0, decimal_places=2)
    valor_mensual: Decimal = Field(gt=0, decimal_places=2)
    fecha_inicio: date
    fecha_fin: date
    supervisor_nombre: str | None = Field(default=None, max_length=255)
    entidad: str | None = Field(default=None, max_length=255)
    dependencia: str | None = Field(default=None, max_length=255)
    documento_proveedor: str | None = Field(default=None, max_length=30)
    obligaciones: list[ObligacionCreate] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "example": {
                "numero_contrato": "CD-045-2025",
                "objeto": "Prestación de servicios profesionales como desarrollador de software para el fortalecimiento de los sistemas de información de la entidad",
                "valor_total": "12000000.00",
                "valor_mensual": "2000000.00",
                "fecha_inicio": "2025-01-01",
                "fecha_fin": "2025-06-30",
                "supervisor_nombre": "María García López",
                "entidad": "Ministerio de Tecnologías de la Información",
                "dependencia": "Dirección de Transformación Digital",
                "documento_proveedor": "1016019452",
                "obligaciones": [
                    {
                        "descripcion": "Desarrollar y mantener los módulos del sistema de información asignados",
                        "tipo": "tecnica",
                        "orden": 1,
                    },
                    {
                        "descripcion": "Presentar informe mensual de actividades al supervisor del contrato",
                        "tipo": "administrativa",
                        "orden": 2,
                    },
                ],
            }
        }
    }


class ContratoUpdate(BaseModel):
    numero_contrato: str | None = Field(default=None, min_length=1, max_length=100)
    objeto: str | None = Field(default=None, min_length=10, max_length=2000)
    valor_total: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    valor_mensual: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    fecha_inicio: date | None = None
    fecha_fin: date | None = None
    supervisor_nombre: str | None = Field(default=None, max_length=255)
    entidad: str | None = Field(default=None, max_length=255)
    dependencia: str | None = Field(default=None, max_length=255)
    documento_proveedor: str | None = Field(default=None, max_length=30)

    model_config = {
        "json_schema_extra": {
            "example": {
                "supervisor_nombre": "Carlos Rodríguez Martínez",
                "valor_mensual": "2500000.00",
            }
        }
    }


class ContratoResponse(BaseModel):
    id: uuid.UUID
    usuario_id: uuid.UUID
    numero_contrato: str
    objeto: str
    valor_total: Decimal
    valor_mensual: Decimal
    fecha_inicio: date
    fecha_fin: date
    supervisor_nombre: str | None
    entidad: str | None
    dependencia: str | None
    documento_proveedor: str | None
    obligaciones: list[ObligacionResponse]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContratoListItem(BaseModel):
    id: uuid.UUID
    numero_contrato: str
    objeto: str
    valor_total: Decimal
    valor_mensual: Decimal
    fecha_inicio: date
    fecha_fin: date
    entidad: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PeriodoPendienteResponse(BaseModel):
    anio: int
    mes: int
    nombre_mes: str
    pendiente: bool


class ContratoContextoAgenteResponse(BaseModel):
    contrato_id: uuid.UUID
    numero_contrato: str
    objeto: str
    entidad: str | None
    dependencia: str | None
    supervisor_nombre: str | None
    fecha_inicio: date
    fecha_fin: date
    valor_total: Decimal
    valor_mensual: Decimal
    documento_proveedor: str | None
    contratista_nombre: str
    contratista_cedula: str | None
    obligaciones: list[ObligacionResponse]
    texto_contrato: str | None
    instrucciones_usuario: str | None
    cuentas_previas: list[dict[str, Any]]
    system_prompt: str | None
    listo: bool
    faltantes: list[str]
