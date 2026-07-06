"""Schemas for the coverage matrix (semáforo) — Modo Simple.

Mirrors the competitor's "Cubiertas / Débiles / Sin evidencia" traffic-light while
encoding the trust rule «sin soporte = rojo, siempre». The coverage is computed
deterministically (no LLM) from obligaciones ↔ actividades ↔ evidencias.
"""

import enum
import uuid

from pydantic import BaseModel, Field


class EstadoCobertura(enum.StrEnum):
    """Per-obligation coverage status."""

    CUBIERTA = "cubierta"  # verde — evidencia + justificación
    DEBIL = "debil"  # amarillo — evidencia pero sin justificación
    SIN_EVIDENCIA = "sin_evidencia"  # rojo — sin soporte documental


# Traffic-light colour used by the frontend semáforo.
COLOR_POR_ESTADO: dict[EstadoCobertura, str] = {
    EstadoCobertura.CUBIERTA: "verde",
    EstadoCobertura.DEBIL: "amarillo",
    EstadoCobertura.SIN_EVIDENCIA: "rojo",
}


class ObligacionCobertura(BaseModel):
    obligacion_id: uuid.UUID
    descripcion: str
    tipo: str
    orden: int
    estado: EstadoCobertura
    color: str = Field(description="verde | amarillo | rojo")
    fuerza: float = Field(ge=0.0, le=1.0, description="Fuerza del soporte (0 sin soporte, 1 fuerte)")
    num_actividades: int
    num_evidencias: int
    tiene_justificacion: bool
    detalle: str = Field(description="Explicación legible del estado de cobertura")


class ResumenCobertura(BaseModel):
    total: int
    cubiertas: int
    debiles: int
    sin_evidencia: int
    porcentaje_cubierto: float = Field(ge=0.0, le=100.0)


class CoberturaResponse(BaseModel):
    cuenta_cobro_id: uuid.UUID
    contrato_id: uuid.UUID
    resumen: ResumenCobertura
    obligaciones: list[ObligacionCobertura]
    listo_para_generar: bool = Field(
        description="True si ninguna obligación quedó en estado 'sin_evidencia'."
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "cuenta_cobro_id": "00000000-0000-0000-0000-000000000000",
                "contrato_id": "00000000-0000-0000-0000-000000000000",
                "resumen": {
                    "total": 3,
                    "cubiertas": 1,
                    "debiles": 1,
                    "sin_evidencia": 1,
                    "porcentaje_cubierto": 33.3,
                },
                "obligaciones": [
                    {
                        "obligacion_id": "00000000-0000-0000-0000-000000000000",
                        "descripcion": "Desarrollar los módulos del sistema de información",
                        "tipo": "especifica",
                        "orden": 1,
                        "estado": "cubierta",
                        "color": "verde",
                        "fuerza": 1.0,
                        "num_actividades": 2,
                        "num_evidencias": 3,
                        "tiene_justificacion": True,
                        "detalle": "Respaldada con evidencia y justificación.",
                    }
                ],
                "listo_para_generar": False,
            }
        }
    }
