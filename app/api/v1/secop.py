"""SECOP public contracting data endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.secop_scraper.port import SecopScraperPort
from app.api.deps import CurrentUser, get_secop_scraper
from app.core.database import get_db
from app.schemas.checklist import CategoriaUpdateBody
from app.schemas.secop import (
    ArchivoComprimidoResponse,
    ScraperFallbackResult,
    SecopConsultaCompletaResponse,
    SecopContratoResponse,
    SecopDocumentoResponse,
    SecopDocumentosConCoberturaResponse,
    SecopImportResult,
    SecopProcesoResponse,
    SecopSincronizarDocumentosResult,
)
from app.services import secop_scraper_service, secop_service

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


# Route compatibility note (Fix C1):
# GET /documentos/{numero_contrato}       — 3-segment path, param is a string
# GET /documentos/{doc_id}/archivos       — 4-segment path with literal suffix "/archivos"
# FastAPI resolves these by path depth: the literal "/archivos" suffix makes the 4-segment
# route unambiguous. No collision exists; no reordering or rename is required.
@router.get("/documentos/{numero_contrato}", response_model=SecopDocumentosConCoberturaResponse)
async def buscar_documentos(
    numero_contrato: str,
    user: CurrentUser,
    refresh: bool = Query(False, description="true → fuerza recarga desde SECOP"),
    db: AsyncSession = Depends(get_db),
) -> SecopDocumentosConCoberturaResponse:
    """Lista los documentos SECOP asociados a un número de contrato (formato CO1.PCCNTR.xxxxxxx).

    El `numero_contrato` corresponde al campo `referencia_del_contrato` del contrato SECOP
    (visible en la respuesta de GET /secop/contratos como `numero_contrato`).

    Incluye un bloque `cobertura` que explica cuándo un conteo bajo de documentos es
    esperado (p. ej. contratos de 2024, con cobertura parcial en datos abiertos) en
    lugar de parecer un error — ver `secop-dataset-ingestion` Slice 2 task 2.10b.
    """
    return await secop_service.obtener_documentos_con_cobertura(db, numero_contrato, refresh=refresh)


@router.get("/documentos/{doc_id}/archivos", response_model=ArchivoComprimidoResponse)
async def listar_archivos_comprimido(
    doc_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ArchivoComprimidoResponse:
    """Lista los archivos internos de un documento comprimido (.zip).

    Descarga el archivo desde `url_descarga` y devuelve el listado de archivos contenidos.
    Para archivos .rar informa que no es posible listar el contenido sin herramientas adicionales.
    """
    return await secop_service.listar_archivos_comprimido(db, doc_id)


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


@router.patch("/documentos/{doc_id}/categoria", response_model=SecopDocumentoResponse)
async def actualizar_categoria_documento(
    doc_id: uuid.UUID,
    body: CategoriaUpdateBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SecopDocumentoResponse:
    """Sobreescribe manualmente la categoría de un documento SECOP.

    Marca `categoria_override=true` para que reclasificaciones automáticas no
    lo sobreescriban en el futuro.
    """
    doc = await secop_service.actualizar_categoria_documento(db, doc_id, body.categoria, usuario_id=user.id)
    await db.commit()
    await db.refresh(doc)
    return SecopDocumentoResponse.model_validate(doc)


@router.post("/explorar-agentico", response_model=ScraperFallbackResult)
async def explorar_agentico(
    user: CurrentUser,
    numero_contrato: str = Query(
        ...,
        description="Número/referencia de contrato SECOP (formato CO1.PCCNTR.xxxxxxx)",
    ),
    scraper: SecopScraperPort = Depends(get_secop_scraper),
    db: AsyncSession = Depends(get_db),
) -> ScraperFallbackResult:
    """Dispara manualmente el fallback de scraping de SECOP II para un contrato.

    Manual únicamente: NUNCA se invoca desde `sincronizar-documentos` ni desde
    la evaluación del checklist. Gateado por `SECOP_SCRAPER_ENABLED` (default
    False) — con la bandera apagada, este endpoint responde 200 sin efecto
    (adaptador nulo). Aplica una cuota horaria por usuario
    (`SECOP_SCRAPER_HOURLY_LIMIT`) antes de invocar el scraper.

    Fail-soft: un captcha o una falla del microservicio de scraping se
    reportan como `estado="captcha_required"`/`"unavailable"` en un 200, no
    como error — el único camino de excepción es la cuota agotada (429).
    """
    return await secop_scraper_service.explorar_documentos_agentico(db, scraper, user.id, numero_contrato)
