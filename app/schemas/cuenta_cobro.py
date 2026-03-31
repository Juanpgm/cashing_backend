"""Schemas for CuentaCobro — create, respond, state transitions, PDF."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.cuenta_cobro import EstadoCuentaCobro


class ActividadCreate(BaseModel):
    descripcion: str = Field(min_length=10, max_length=2000)
    justificacion: str | None = Field(default=None, max_length=3000)
    fecha_realizacion: date | None = None
    obligacion_id: uuid.UUID | None = None

    model_config = {
        "json_schema_extra": {
            "example": {
                "descripcion": "Desarrollo e implementación del módulo de autenticación con OAuth 2.0 para el sistema de información institucional",
                "justificacion": "Se cumple con la obligación técnica de desarrollar los módulos del sistema. Se entregó documentación técnica y código fuente al supervisor.",
                "fecha_realizacion": "2025-03-15",
                "obligacion_id": None,
            }
        }
    }


class ActividadResponse(BaseModel):
    id: uuid.UUID
    descripcion: str
    justificacion: str | None
    fecha_realizacion: date | None
    obligacion_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class CuentaCobroCreate(BaseModel):
    contrato_id: uuid.UUID
    mes: int = Field(ge=1, le=12)
    anio: int = Field(ge=2020, le=2099)
    valor: Decimal = Field(gt=0, decimal_places=2)

    model_config = {
        "json_schema_extra": {
            "example": {
                "contrato_id": "00000000-0000-0000-0000-000000000000",
                "mes": 3,
                "anio": 2025,
                "valor": "2000000.00",
            }
        }
    }


class CuentaCobroResponse(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    mes: int
    anio: int
    estado: EstadoCuentaCobro
    valor: Decimal
    pdf_storage_key: str | None
    fecha_envio: datetime | None
    actividades: list[ActividadResponse]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CuentaCobroListItem(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    mes: int
    anio: int
    estado: EstadoCuentaCobro
    valor: Decimal
    pdf_storage_key: str | None
    fecha_envio: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ActividadesBulkCreate(BaseModel):
    """Crea varias actividades de una vez, vinculándolas opcionalmente a obligaciones."""

    actividades: list[ActividadCreate] = Field(
        min_length=1,
        max_length=50,
        description="Lista de actividades a crear. Máximo 50 por llamada.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "actividades": [
                    {
                        "descripcion": "Desarrollé e implementé el módulo de autenticación OAuth 2.0 para el sistema institucional",
                        "justificacion": "Cumplimiento de la obligación técnica de desarrollar los módulos del sistema de información asignados",
                        "fecha_realizacion": "2025-03-05",
                        "obligacion_id": None,
                    },
                    {
                        "descripcion": "Presenté informe mensual de avance al supervisor del contrato con resultados de las actividades ejecutadas",
                        "justificacion": "Cumplimiento de la obligación administrativa de presentar informes mensuales de avance",
                        "fecha_realizacion": "2025-03-28",
                        "obligacion_id": None,
                    },
                    {
                        "descripcion": "Asistí a reunión de seguimiento del proyecto con el equipo técnico de la entidad",
                        "justificacion": "Coordinación técnica requerida para la correcta ejecución del objeto contractual",
                        "fecha_realizacion": "2025-03-15",
                        "obligacion_id": None,
                    },
                ]
            }
        }
    }


class ActividadesDesdeTextoRequest(BaseModel):
    """Crea actividades a partir de un listado de texto numerado.

    Cada línea con número (1. / 1) / 1-) se convierte en una actividad.
    Si el contrato tiene obligaciones, se puede vincular automáticamente
    por posición (actividad 1 → obligación 1, etc.).
    """

    texto: str = Field(
        min_length=10,
        max_length=10000,
        description=(
            "Listado numerado de actividades. Cada ítem debe empezar con un número seguido de "
            "punto, paréntesis o guion. Ejemplo:\n"
            "1. Elaboré el informe de avance mensual\n"
            "2. Asistí a reunión de seguimiento\n"
            "3. Desarrollé el módulo de reportes"
        ),
    )
    fecha_realizacion: date | None = Field(
        default=None,
        description="Fecha de realización que se asignará a todas las actividades del listado.",
    )
    vincular_obligaciones: bool = Field(
        default=True,
        description=(
            "Si es true y el contrato tiene obligaciones registradas, cada actividad se vincula "
            "por posición a su obligación correspondiente (actividad 1 → obligación 1, etc.)."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "texto": (
                    "1. Desarrollé e implementé el módulo de autenticación con OAuth 2.0\n"
                    "2. Elaboré y presenté el informe mensual de actividades al supervisor\n"
                    "3. Asistí a reunión de seguimiento del proyecto con el equipo técnico\n"
                    "4. Realicé pruebas de integración y corrección de errores en el sistema"
                ),
                "fecha_realizacion": "2025-03-31",
                "vincular_obligaciones": True,
            }
        }
    }


class ActividadesBulkResponse(BaseModel):
    creadas: int
    actividades: list[ActividadResponse]


class CambiarEstadoRequest(BaseModel):
    estado: EstadoCuentaCobro

    model_config = {
        "json_schema_extra": {
            "example": {"estado": "enviada"}
        }
    }


class GenerarPDFResponse(BaseModel):
    pdf_url: str
    pdf_storage_key: str


class PDFUrlResponse(BaseModel):
    pdf_url: str
