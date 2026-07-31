"""Plantillas de organismo API — clone-oriented listing and ingestion per contrato.

Thin delegates to `requisito_inference_service` (ownership is checked there,
same pattern as the other contrato-scoped services). Ingestion failure is
graceful degradation: `clonable=false` + avisos, never an error response.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.core.file_validation import MAX_FILE_SIZE_BYTES, validate_file_size
from app.models.plantilla_organismo import PlantillaOrganismo
from app.schemas.plantilla_organismo import (
    AnalizarArchivoResponse,
    CampoResponse,
    ConfirmarArchivoRequest,
    ConfirmarArchivoResponse,
    PlantillaOrganismoIngestRequest,
    PlantillaOrganismoResponse,
)
from app.services import requisito_inference_service

logger = structlog.get_logger("api.plantillas_organismo")

_ARCHIVO_EXTENSIONS = {".zip", ".rar"}

router = APIRouter(prefix="/contratos/{contrato_id}/plantillas-organismo", tags=["plantillas-organismo"])


def _to_response(plantilla: PlantillaOrganismo, avisos: list[str] | None = None) -> PlantillaOrganismoResponse:
    estructura = plantilla.estructura_json or {}
    campos: dict[str, list[CampoResponse]] = {}
    for campo in estructura.get("campos") or []:
        clave = campo.get("campo") or ""
        scope = clave.split(".", 1)[0] or "otro"
        campos.setdefault(scope, []).append(
            CampoResponse(campo=clave, etiqueta=campo.get("etiqueta") or "", modo=campo.get("modo") or "cell")
        )
    return PlantillaOrganismoResponse(
        id=plantilla.id,
        tipo_documento=plantilla.tipo_documento,
        formato=plantilla.formato,
        clonable=bool(estructura.get("clonable")),
        avisos=[*(estructura.get("avisos") or []), *(avisos or [])],
        fuente_documento_id=plantilla.fuente_documento_id,
        campos=campos,
    )


@router.get("/", response_model=list[PlantillaOrganismoResponse])
async def listar_plantillas_organismo(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PlantillaOrganismoResponse]:
    """List every ingested plantilla for the contrato's contracting entity."""
    plantillas = await requisito_inference_service.listar_plantillas_organismo(db, user.id, contrato_id)
    return [_to_response(p) for p in plantillas]


@router.post("/", response_model=PlantillaOrganismoResponse)
async def ingerir_plantilla_organismo(
    contrato_id: uuid.UUID,
    data: PlantillaOrganismoIngestRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PlantillaOrganismoResponse:
    """Ingest an already-uploaded documento as the organism's plantilla.

    Structure/campo extraction degrades gracefully: on failure the response
    carries `clonable=false` plus avisos instead of an error.
    """
    plantilla, avisos = await requisito_inference_service.ingerir_plantilla_organismo(
        db, user.id, contrato_id, data.documento_fuente_id
    )
    if plantilla is None:
        return PlantillaOrganismoResponse(clonable=False, avisos=avisos)
    return _to_response(plantilla, avisos)


@router.post("/archivo:analizar", response_model=AnalizarArchivoResponse)
async def analizar_archivo_organismo(
    contrato_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
) -> AnalizarArchivoResponse:
    """Expand a `.zip`/`.rar` bundle of organism templates and propose a
    `tipo_documento` per member. Nothing is ingested until `archivo:confirmar`.
    """
    if not file.filename or Path(file.filename).suffix.lower() not in _ARCHIVO_EXTENSIONS:
        raise ValidationError("Se espera un archivo .zip o .rar.")
    content = await file.read()
    if not validate_file_size(len(content)):
        max_mb = MAX_FILE_SIZE_BYTES // (1024 * 1024)
        raise ValidationError(f"El archivo excede el tamaño máximo de {max_mb}MB.")
    propuestas, avisos = await requisito_inference_service.analizar_archivo_organismo(
        db, user.id, contrato_id, file.filename, content
    )
    return AnalizarArchivoResponse(propuestas=propuestas, avisos=avisos)


@router.post("/archivo:confirmar", response_model=ConfirmarArchivoResponse)
async def confirmar_archivo_organismo(
    contrato_id: uuid.UUID,
    data: ConfirmarArchivoRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConfirmarArchivoResponse:
    """Bounded-concurrency ingestion of the user-confirmed archive members."""
    resultados, avisos = await requisito_inference_service.confirmar_archivo_organismo(
        db, user.id, contrato_id, data.miembros
    )
    return ConfirmarArchivoResponse(resultados=resultados, avisos=avisos)
