"""Schemas for CuentaCobro — create, respond, state transitions, PDF."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.models.cuenta_cobro import EstadoCuentaCobro, PosicionCuota
from app.schemas.coherence import FindingOut


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
    anio: int = Field(ge=2000, le=2099)
    numero_cuota: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Optional explicit override for the 1-based cuota number. Use to migrate a "
            "contrato that already had cuotas tracked outside the app — the first cuota "
            "registered in-app can start above 1. Omit to keep the default: count of "
            "existing active cuotas + 1. Colliding with another active cuota of the same "
            "contrato raises CUOTA_NUMERO_CONFLICT."
        ),
    )
    valor: Decimal | None = Field(
        default=None,
        gt=0,
        decimal_places=2,
        description="Optional. Defaults to contrato.valor_mensual when not provided.",
    )
    informe_final: bool = Field(
        default=False,
        description=(
            "Marks this cuota as the contract's final one (billing-resilience-templates "
            "slice #3). Never inferred — only one cuota per contrato may have this set; "
            "a second attempt raises CUOTA_POSITION_CONFLICT."
        ),
    )
    fecha_transaccion: date | None = Field(
        default=None,
        description=(
            "Optional transaction/payment date (radicacion-stepper, work unit B1). "
            "Captured at stepper step 2; used as the bounding date for month-scoped "
            "pipelines when present, falling back to mes/anio when absent."
        ),
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "contrato_id": "00000000-0000-0000-0000-000000000000",
                "mes": 3,
                "anio": 2025,
                "valor": "2000000.00",
                "informe_final": False,
                "fecha_transaccion": "2025-03-31",
            }
        }
    }


class CuentaCobroUpdate(BaseModel):
    """Partial update for a CuentaCobro. Only allowed when in BORRADOR state."""

    mes: int | None = Field(default=None, ge=1, le=12)
    anio: int | None = Field(default=None, ge=2000, le=2099)
    valor: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    informe_final: bool | None = Field(
        default=None,
        description=(
            "Set/unset the explicit final-cuota flag (billing-resilience-templates "
            "slice #3). Setting `true` when another cuota of this contrato already has "
            "it set raises CUOTA_POSITION_CONFLICT."
        ),
    )
    fecha_transaccion: date | None = Field(
        default=None,
        description=(
            "Transaction/payment date (radicacion-stepper, work unit B1). Editable in "
            "stepper step-2 resume mode. Because null is a meaningful value (clears the "
            "date), the service distinguishes 'omitted' from 'explicit null' via "
            "model_fields_set — send the field to set/clear it, omit it to leave it untouched."
        ),
    )
    contexto_usuario: str | None = Field(
        default=None,
        max_length=2000,
        description=(
            "Free-text summary of what the contractor did this month. Optional grounding "
            "for LLM activity/justification generation. Null is meaningful (clears the "
            "text): the service keys off model_fields_set, like fecha_transaccion."
        ),
    )
    consecutivo_ds: str | None = Field(
        default=None,
        max_length=50,
        description=(
            "Entity payment consecutive for the Documento Soporte xlsx (G4). Editable in "
            "stepper step-5 (Formato). Null is meaningful (clears it): the service keys off "
            "model_fields_set, like fecha_transaccion."
        ),
    )

    model_config = {"json_schema_extra": {"example": {"mes": 4, "anio": 2026, "valor": "2500000.00"}}}


class CuentaCobroResponse(BaseModel):
    id: uuid.UUID
    contrato_id: uuid.UUID
    mes: int
    anio: int
    estado: EstadoCuentaCobro
    valor: Decimal
    pdf_storage_key: str | None
    fecha_envio: datetime | None
    numero_cuota: int | None
    posicion: PosicionCuota
    informe_final: bool
    fecha_transaccion: date | None
    contexto_usuario: str | None
    consecutivo_ds: str | None
    actividades: list[ActividadResponse]
    created_at: datetime
    updated_at: datetime
    advertencias_coherencia: list[FindingOut] | None = Field(
        default=None,
        description=(
            "SOFT coherence findings surfaced at radicar time (billing-resilience-"
            "templates slice #3, task 3.12c) — only populated on the response returned "
            "by POST /radicar when the coherence validator found non-blocking warnings. "
            "None/absent in every other response."
        ),
    )

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
    numero_cuota: int | None
    posicion: PosicionCuota
    informe_final: bool
    fecha_transaccion: date | None
    consecutivo_ds: str | None
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

    model_config = {"json_schema_extra": {"example": {"estado": "enviada"}}}


class GenerarPDFResponse(BaseModel):
    pdf_url: str
    pdf_storage_key: str


class PDFUrlResponse(BaseModel):
    pdf_url: str
