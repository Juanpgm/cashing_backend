"""SECOP public contracting data endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.secop import (
    SecopConsultaCompletaResponse,
    SecopContratoResponse,
    SecopDocumentoResponse,
    SecopImportResult,
    SecopProcesoResponse,
    SecopSincronizarDocumentosResult,
)
from app.services import secop_service

router = APIRouter(prefix="/secop", tags=["secop"])

_CEDULA_DESC = "Número de cédula del contratista (5-15 dígitos)"
_CEDULA_EXAMPLE = "1016019452"


@router.get("/contratos", response_model=list[SecopContratoResponse])
async def buscar_contratos(
    user: CurrentUser,
    cedula: str = Query(..., description=_CEDULA_DESC, pattern=r"^\d{5,15}$", example=_CEDULA_EXAMPLE),
    refresh: bool = Query(False, description="true → fuerza recarga desde SECOP ignorando el caché de 24h"),
    db: AsyncSession = Depends(get_db),
) -> list[SecopContratoResponse]:
    """Busca contratos de prestación de servicios en SECOP asociados a una cédula.

    Los resultados se cachean por 24 h en la tabla `secop_contratos`.
    Usa `refresh=true` para forzar actualización desde Socrata.
    """
    return await secop_service.buscar_contratos_cedula(db, cedula, refresh=refresh)


@router.get("/procesos/{id_proceso}", response_model=SecopProcesoResponse | None)
async def obtener_proceso(
    id_proceso: str,
    user: CurrentUser,
    refresh: bool = Query(False, description="true → fuerza recarga desde SECOP"),
    db: AsyncSession = Depends(get_db),
) -> SecopProcesoResponse | None:
    """Obtiene un proceso de contratación SECOP por su ID (formato CO1.BDOS.xxxxxxx).

    El `id_proceso` corresponde al campo `proceso_de_compra` del contrato SECOP.
    """
    return await secop_service.obtener_proceso(db, id_proceso, refresh=refresh)


@router.get("/documentos/{numero_contrato}", response_model=list[SecopDocumentoResponse])
async def buscar_documentos(
    numero_contrato: str,
    user: CurrentUser,
    refresh: bool = Query(False, description="true → fuerza recarga desde SECOP"),
    db: AsyncSession = Depends(get_db),
) -> list[SecopDocumentoResponse]:
    """Lista los documentos SECOP asociados a un número de contrato (formato CO1.PCCNTR.xxxxxxx).

    El `numero_contrato` corresponde al campo `referencia_del_contrato` del contrato SECOP
    (visible en la respuesta de GET /secop/contratos como `numero_contrato`).
    """
    return await secop_service.buscar_documentos_contrato(db, numero_contrato, refresh=refresh)


@router.post("/importar", response_model=SecopImportResult, status_code=201)
async def importar_contratos(
    user: CurrentUser,
    documento_proveedor: str = Query(
        ...,
        description="Cédula o NIT del contratista (5-15 dígitos)",
        pattern=r"^\d{5,15}$",
        example=_CEDULA_EXAMPLE,
    ),
    confirmar: bool = Query(
        False,
        description=(
            "**false** → preview: muestra los contratos que se importarían sin guardar nada. "
            "**true** → guarda los contratos nuevos en la tabla `contratos` del usuario."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> SecopImportResult:
    """Importa todos los contratos SECOP del `documento_proveedor` a la tabla de contratos del usuario.

    - `confirmar=false` (defecto): devuelve vista previa de los contratos a importar sin persistir nada.
    - `confirmar=true`: guarda los contratos nuevos; los duplicados (mismo `numero_contrato`) se omiten.
    """
    return await secop_service.importar_contratos_secop(db, documento_proveedor, user.id, confirmar=confirmar)


@router.post("/sincronizar-documentos", response_model=SecopSincronizarDocumentosResult)
async def sincronizar_documentos(
    user: CurrentUser,
    cedula: str = Query(
        ...,
        description=_CEDULA_DESC,
        pattern=r"^\d{5,15}$",
        example=_CEDULA_EXAMPLE,
    ),
    confirmar: bool = Query(
        False,
        description=(
            "**false** → preview: muestra los documentos que se importarían sin guardar nada. "
            "**true** → guarda los documentos vinculados por FK a sus contratos y procesos SECOP."
        ),
    ),
    db: AsyncSession = Depends(get_db),
) -> SecopSincronizarDocumentosResult:
    """Sincroniza todos los documentos SECOP de los contratos y procesos cacheados de una cédula.

    Requiere haber ejecutado previamente `GET /secop/contratos` o `POST /secop/importar`
    para que los contratos estén en la caché. Los documentos quedan vinculados por FK
    a `secop_contratos` y `secop_procesos`.
    """
    return await secop_service.sincronizar_documentos_secop(db, cedula, confirmar=confirmar)


@router.get("/consulta", response_model=SecopConsultaCompletaResponse)
async def consulta_completa(
    user: CurrentUser,
    cedula: str = Query(..., description=_CEDULA_DESC, pattern=r"^\d{5,15}$", example=_CEDULA_EXAMPLE),
    refresh: bool = Query(False, description="true → fuerza recarga desde SECOP"),
    db: AsyncSession = Depends(get_db),
) -> SecopConsultaCompletaResponse:
    """Consulta completa: contratos + proceso + documentos por cédula en una sola llamada."""
    return await secop_service.consulta_completa(db, cedula, refresh=refresh)
