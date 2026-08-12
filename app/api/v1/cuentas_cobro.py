"""CuentasCobro API — CRUD, state machine, PDF generation, and preview."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import date
from typing import cast

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.catalog  # noqa: F401 — import-for-side-effect: populates TOOL_REGISTRY
from app.adapters.storage.s3_adapter import S3StorageAdapter
from app.api.deps import CurrentUser, get_pdf_storage
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.core.file_validation import MAX_EVIDENCE_FILES_PER_REQUEST
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.schemas.cobertura import CoberturaResponse
from app.schemas.coherence import FindingOut
from app.schemas.cuenta_cobro import (
    ActividadCreate,
    ActividadesBulkCreate,
    ActividadesBulkResponse,
    ActividadesDesdeTextoRequest,
    ActividadResponse,
    CambiarEstadoRequest,
    CuentaCobroCreate,
    CuentaCobroListItem,
    CuentaCobroResponse,
    CuentaCobroUpdate,
    GenerarPDFResponse,
    PDFUrlResponse,
)
from app.schemas.evidencia import EvidenciasCuentaSubidaResponse
from app.schemas.evidencia_clasificacion import (
    ClasificacionEstadoResponse,
    ClasificacionJobResponse,
    ReclasificacionRequest,
    ReclasificacionResponse,
)
from app.schemas.google_workspace import (
    EvidencePersistRequest,
    EvidencePersistSummary,
    EvidenciasAutoResponse,
)
from app.schemas.paquete import PaqueteInfoResponse
from app.schemas.plantilla_organismo import FormatoValoresResponse
from app.schemas.radicacion_prep import PreparaRadicacionResponse
from app.schemas.stepper_state import StepperStateResponse
from app.services import (
    cobertura_service,
    constancia_service,
    cruzar_service,
    cuenta_cobro_service,
    evidence_auto_service,
    evidence_classification_service,
    evidence_persist_service,
    evidencia_service,
    informe_service,
    paquete_service,
    pdf_signature_service,
    stepper_state_service,
)
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool

logger = structlog.get_logger("api.cuentas_cobro")

router = APIRouter(prefix="/cuentas-cobro", tags=["cuentas-cobro"])


@router.post("/", response_model=CuentaCobroResponse, status_code=status.HTTP_201_CREATED)
async def crear_cuenta_cobro(
    data: CuentaCobroCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """Create a new CuentaCobro (costs 10 credits). Starts in BORRADOR state."""
    return await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)


@router.get("/", response_model=list[CuentaCobroListItem])
async def listar_cuentas_cobro(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    contrato_id: uuid.UUID | None = None,
) -> list[CuentaCobroListItem]:
    """List CuentasCobro for the authenticated user, newest first.

    When ``contrato_id`` is provided, only that contract's cuentas are returned
    so each contract page shows exclusively its own cuentas de cobro.
    """
    return await cuenta_cobro_service.listar_cuentas_cobro(db, user.id, contrato_id)


@router.get("/{cuenta_id}", response_model=CuentaCobroResponse)
async def obtener_cuenta_cobro(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """Get a single CuentaCobro with its activities."""
    return await cuenta_cobro_service.obtener_cuenta_cobro(db, user.id, cuenta_id)


@router.get("/{cuenta_id}/stepper-state", response_model=StepperStateResponse)
async def obtener_stepper_state(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StepperStateResponse:
    """Aggregate, read-only readiness for the `/radicar` 7-step wizard.

    Composes existing read-only services only (never `preparar_radicacion`):
    no credits are consumed, no writes happen, no package is generated.
    `numero_cuota != null` in the response is the idempotency signal the
    frontend uses to avoid re-charging credits on resume (radicacion-stepper,
    work unit B4 — see design section 1-2 for the full contract and the
    contiguous-prefix resume algorithm).
    """
    return await stepper_state_service.obtener_stepper_state(db, user.id, cuenta_id)


@router.get("/{cuenta_id}/paquete", response_model=PaqueteInfoResponse)
async def obtener_paquete(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PaqueteInfoResponse:
    """Read-only package listing: current package metadata + LISTO/PENDIENTE
    content summary, without regenerating anything.

    `existe: false` when no package has been generated for this cuota yet.
    Produces no storage writes and no packaging call — only a storage-prefix
    read via `StoragePort` (radicacion-stepper, work unit B5).
    """
    return await paquete_service.listar_paquete(db, user.id, cuenta_id)


@router.post("/{cuenta_id}/paquete/regenerar", response_model=PreparaRadicacionResponse)
async def regenerar_paquete(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PreparaRadicacionResponse:
    """Regenerate the evidence package: thin delegate to `radicacion_prep_
    service.preparar_radicacion` — the single fail-closed readiness gate
    (checklist -> coherence HARD-stop -> packager with secret scan).

    Re-running against the same cuota overwrites the deterministic storage
    key (radicacion-stepper, work unit B5 — design section 6). Never bypasses
    the gate: `CHECKLIST_INCOMPLETE`, `COHERENCE_CHECK_FAILED`, `PACKAGE_
    PENDIENTE`, and `SECRET_DETECTED_IN_PACKAGE` all still raise through here.
    """
    resultado = await paquete_service.regenerar_paquete(db, user.id, cuenta_id)
    return PreparaRadicacionResponse(
        cuenta_cobro_id=resultado.cuenta_cobro_id,
        storage_key=resultado.storage_key,
        filename=resultado.filename,
        size_bytes=resultado.size_bytes,
        listo_para_radicar=resultado.listo_para_radicar,
        pendientes=resultado.pendientes,
        advertencias_coherencia=[FindingOut(**asdict(f)) for f in resultado.advertencias_coherencia],
        es_borrador=resultado.es_borrador,
    )


@router.get("/{cuenta_id}/paquete/descargar", response_class=Response)
async def descargar_paquete(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Pure read (P5): streams the persisted, gate-validated package bytes
    from the deterministic storage key — never re-runs `preparar_radicacion`
    or the LLM-backed informe step, so repeated downloads with no explicit
    regenerate in between are byte-identical.

    `NotFoundError` (404) when nothing has been generated yet for this cuota;
    the client should call `POST /paquete/regenerar` first (mirrors the
    `evidencias.zip` route's `Response` shape, but reads instead of
    regenerating).
    """
    content, filename = await paquete_service.descargar_paquete(db, user.id, cuenta_id)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{cuenta_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def eliminar_cuenta_cobro(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> None:
    """Hard-delete a CuentaCobro and everything scoped to it (actividades,
    evidencias, files). Allowed in borrador, rechazada, or enviada; blocked once
    aprobada or pagada (settled/paid financial record)."""
    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta_id, storage)


@router.patch("/{cuenta_id}", response_model=CuentaCobroResponse)
async def actualizar_cuenta_cobro(
    cuenta_id: uuid.UUID,
    data: CuentaCobroUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """Partial update (mes/anio/valor). Only allowed in BORRADOR state."""
    return await cuenta_cobro_service.actualizar_cuenta_cobro(db, user.id, cuenta_id, data)


@router.post("/{cuenta_id}/actividades", response_model=ActividadResponse, status_code=status.HTTP_201_CREATED)
async def agregar_actividad(
    cuenta_id: uuid.UUID,
    data: ActividadCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActividadResponse:
    """Add an activity to a CuentaCobro. Only allowed in BORRADOR or RECHAZADA states."""
    return await cuenta_cobro_service.agregar_actividad(db, user.id, cuenta_id, data)


@router.post(
    "/{cuenta_id}/actividades/bulk",
    response_model=ActividadesBulkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def agregar_actividades_bulk(
    cuenta_id: uuid.UUID,
    data: ActividadesBulkCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActividadesBulkResponse:
    """Add multiple activities at once. Accepts 1-50 activities per call."""
    return await cuenta_cobro_service.agregar_actividades_bulk(db, user.id, cuenta_id, data.actividades)


@router.post(
    "/{cuenta_id}/actividades/desde-texto",
    response_model=ActividadesBulkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def agregar_actividades_desde_texto(
    cuenta_id: uuid.UUID,
    data: ActividadesDesdeTextoRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActividadesBulkResponse:
    """Parse a numbered text list and create one activity per line.

    Each line must start with a number followed by `.`, `)`, or `-`.
    If vincular_obligaciones=True and the contract has obligations, each activity
    is automatically linked by position (line 1 → obligación 1, etc.).
    """
    return await cuenta_cobro_service.agregar_actividades_desde_texto(
        db,
        user.id,
        cuenta_id,
        texto=data.texto,
        fecha_realizacion=data.fecha_realizacion,
        vincular_obligaciones=data.vincular_obligaciones,
    )


@router.post(
    "/{cuenta_id}/actividades/generar",
    response_model=ActividadesBulkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def generar_actividades_agente(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    periodo_inicio: date | None = Query(
        default=None, description="YYYY-MM-DD — inicio opcional del período acotado a justificar."
    ),
    periodo_fin: date | None = Query(
        default=None, description="YYYY-MM-DD — fin opcional del período acotado a justificar."
    ),
) -> ActividadesBulkResponse:
    """Use the AI agent to generate and persist activities for this CuentaCobro.

    The agent reads the contract's registered obligations and/or uploaded contract
    document, then generates one activity with justification per obligation.

    Requirements (at least one must be met):
    - The contract has obligations registered (POST /contratos/{id}/obligaciones), OR
    - A contract document has been uploaded (POST /documentos/upload?tipo=contrato).

    If neither is available, use POST /actividades/desde-texto to enter activities manually.

    Optional `periodo_inicio`/`periodo_fin` bound the period surfaced to the agent
    (month-scoping). Omitting both preserves the previous, unscoped behavior.
    """
    return await cuenta_cobro_service.generar_actividades_agente(
        db, user.id, cuenta_id, periodo_inicio=periodo_inicio, periodo_fin=periodo_fin
    )


@router.post(
    "/{cuenta_id}/actividades/desde-obligaciones",
    response_model=ActividadesBulkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def crear_actividades_desde_obligaciones(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ActividadesBulkResponse:
    """Seed one activity per contract obligation, deterministically (NO AI, no LLM key).

    Each obligation becomes a baseline activity (description = the obligation text) that
    the user then edits. Use this to unblock informe generation when only obligaciones
    were loaded. Requires the contract to have obligations and the cuenta in borrador/rechazada.
    """
    return await cuenta_cobro_service.crear_actividades_desde_obligaciones(db, user.id, cuenta_id)


@router.get("/{cuenta_id}/cobertura", response_model=CoberturaResponse)
async def obtener_cobertura(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CoberturaResponse:
    """Compute the coverage matrix (semáforo) for a CuentaCobro.

    For each contract obligation, returns its coverage status —
    ``cubierta`` (verde), ``debil`` (amarillo) or ``sin_evidencia`` (rojo) —
    based on its linked activities, justifications and attached evidence.
    Encodes the trust rule «sin soporte = rojo». No credits consumed.
    """
    return await cobertura_service.calcular_cobertura(db, user.id, cuenta_id)


@router.post("/{cuenta_id}/cruzar", response_model=CoberturaResponse)
async def cruzar_documentos(
    cuenta_id: uuid.UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CoberturaResponse:
    """Match uploaded documents to contract obligations and populate Actividad records.

    Loads all DocumentoFuente records with extracted text for the contract, runs
    keyword + LLM matching against each obligation, and creates one Actividad per
    relevant document-obligation pair. Existing Actividades are deleted before
    re-running (idempotent refresh). Returns the updated coverage matrix.
    """
    return await cruzar_service.cruzar_documentos(db, current_user.id, cuenta_id)


@router.post("/{cuenta_id}/evidencias/persistir", response_model=EvidencePersistSummary)
async def persistir_evidencias(
    cuenta_id: uuid.UUID,
    data: EvidencePersistRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EvidencePersistSummary:
    """Persist evidence discovered by POST /integraciones/evidencias/descubrir.

    The frontend posts back the `obligaciones` list it received from the
    discovery agent. For each entry, upserts one Actividad (linked by
    obligacion_id, never overwriting a user-written justificación) and creates
    one link-type Evidencia per evidence link found. Idempotent — re-persisting
    the same result does not duplicate rows.
    """
    return await evidence_persist_service.persistir_evidencias(db, current_user.id, cuenta_id, data.obligaciones)


@router.post("/{cuenta_id}/evidencias/auto", response_model=EvidenciasAutoResponse)
async def auto_evidencias(
    cuenta_id: uuid.UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EvidenciasAutoResponse:
    """P1 one-action fused evidencias flow: discovery → persistencia → justificación.

    Collapses the 2-3 manual clicks (Descubrir evidencias → Guardar todo → Generar
    justificaciones) into a single call. Idempotent: repeat calls never duplicate
    evidence rows and never destructively overwrite an already-justified actividad
    (see `evidence_auto_service.auto_evidencias`). The manual buttons remain
    available, unchanged, as a fallback.
    """
    return await evidence_auto_service.auto_evidencias(db, current_user.id, cuenta_id)


@router.post(
    "/{cuenta_id}/evidencias/subir",
    response_model=EvidenciasCuentaSubidaResponse,
    status_code=status.HTTP_201_CREATED,
)
async def subir_evidencias_cuenta(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> EvidenciasCuentaSubidaResponse:
    """Sube uno o varios archivos de evidencia para una cuenta de cobro, ANTES de
    que existan actividades (paso Evidencias, ahora anterior a Justificaciones).

    Cada archivo se clasifica automáticamente contra las obligaciones del
    contrato y se adjunta a una actividad "stub" (creada o reutilizada) para que
    la generación de justificaciones y el paquete de radicación puedan usar esa
    clasificación más adelante. Todos los archivos se validan antes de persistir
    cualquiera; un archivo inválido rechaza el lote completo.

    Un `.zip`/`.rar` entre los archivos se expande server-side y cada miembro
    entra al mismo pipeline que un archivo de carpeta; el comprimido en sí
    nunca se almacena. Los miembros omitidos (basura, vacíos, extensión
    bloqueada) o el truncado por límite de miembros se reportan en `avisos`,
    sin rechazar el lote.

    La clasificación multi-obligación (tabla `evidencia_obligacion`) se ejecuta
    en segundo plano (evidence-classification-jobs: Fast upload response) — esta
    respuesta vuelve antes de que termine. Consulte el progreso en
    GET .../evidencias/clasificacion.
    """
    if len(files) > MAX_EVIDENCE_FILES_PER_REQUEST:
        raise ValidationError(
            f"Máximo {MAX_EVIDENCE_FILES_PER_REQUEST} archivos por solicitud (se enviaron {len(files)})."
        )
    archivos = [
        (
            f.filename or "upload",
            f.content_type or "application/octet-stream",
            await f.read(),
        )
        for f in files
    ]
    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_id,
        archivos=archivos,
        background_tasks=background_tasks,
    )
    return EvidenciasCuentaSubidaResponse(resultados=resultados, avisos=resultados.avisos)


@router.post(
    "/{cuenta_id}/evidencias/clasificar",
    response_model=ClasificacionJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def clasificar_evidencias_cuenta(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> ClasificacionJobResponse:
    """(Re)triggers background classification for every not-yet-linked evidencia
    of this cuenta. Idempotent and retry-safe: also the way to retry a
    `failed` job — re-processes whatever is still unresolved, including files
    that failed on a previous run (evidence-classification-jobs: Retryable
    failure state).
    """
    await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)
    job = await evidence_classification_service.encolar_clasificacion(db, background_tasks, cuenta_id)
    return ClasificacionJobResponse.model_validate(job)


@router.get(
    "/{cuenta_id}/evidencias/clasificacion",
    response_model=ClasificacionEstadoResponse,
)
async def obtener_clasificacion_evidencias(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ClasificacionEstadoResponse:
    """Per-file/per-obligación classification progress for polling
    (evidence-classification-jobs: Per-file/per-obligación progress status
    endpoint)."""
    return await evidence_classification_service.obtener_estado_clasificacion(db, user.id, cuenta_id)


@router.patch(
    "/{cuenta_id}/evidencias/{evidencia_id}/obligaciones",
    response_model=ReclasificacionResponse,
)
async def reclasificar_evidencia_obligaciones(
    cuenta_id: uuid.UUID,
    evidencia_id: uuid.UUID,
    data: ReclasificacionRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ReclasificacionResponse:
    """Full user authority over one evidencia's AI-suggested obligación links —
    add/remove/confirm any link, or mark it as having no applicable obligación,
    no approval workflow (evidence-reclassification capability)."""
    links = await evidencia_service.reclasificar_evidencia(
        db,
        usuario_id=user.id,
        cuenta_id=cuenta_id,
        evidencia_id=evidencia_id,
        add=data.add,
        remove=data.remove,
        confirm=data.confirm,
        no_aplica=data.no_aplica,
    )
    return ReclasificacionResponse(
        evidencia_id=evidencia_id,
        obligaciones=[
            {
                "obligacion_id": link.obligacion_id,
                "confianza": link.confianza,
                "score": link.score,
                "status": link.status,
                "source": link.source,
                "reasoning": link.reasoning,
            }
            for link in links
        ],
    )


@router.get(
    "/{cuenta_id}/constancia.pdf",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "PDF constancia"}},
)
async def descargar_constancia(
    cuenta_id: uuid.UUID,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Generate and download a PDF constancia of contractual obligation fulfillment.

    The PDF includes contract metadata, checklist status, activities performed,
    and visual signature blocks for the contractor and supervisor. When PDF
    signing is enabled (``PDF_SIGNATURE_ENABLED``), a PAdES digital signature is
    applied before returning.
    """
    pdf_bytes, filename = await constancia_service.generar_constancia_pdf(db, current_user.id, cuenta_id)
    if pdf_signature_service.firma_activa():
        pdf_bytes = await pdf_signature_service.firmar_pdf(pdf_bytes)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{cuenta_id}/estado", response_model=CuentaCobroResponse)
async def cambiar_estado(
    cuenta_id: uuid.UUID,
    data: CambiarEstadoRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """
    Transition a CuentaCobro to a new state.

    Valid transitions:
    - enviada → aprobada | rechazada | borrador (reopen)
    - rechazada → borrador
    - aprobada → pagada

    Reaching ENVIADA is intentionally rejected here: use POST /radicar instead,
    which enforces the document checklist gate before allowing the transition
    (both from borrador and from rechazada / resubmission). Letting this
    endpoint set estado=enviada directly would bypass that gate entirely.
    """
    if data.estado == EstadoCuentaCobro.ENVIADA:
        raise ValidationError(
            "No se puede establecer el estado 'enviada' directamente vía PATCH /estado. "
            "Use POST /cuentas-cobro/{id}/radicar, que valida el checklist de documentos "
            "antes de radicar."
        )
    return await cuenta_cobro_service.cambiar_estado(db, user.id, cuenta_id, data.estado)


@router.post("/{cuenta_id}/radicar", response_model=CuentaCobroResponse)
async def radicar_cuenta(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> CuentaCobroResponse:
    """
    Radicar (submit) a CuentaCobro: transitions it to ENVIADA once its document
    checklist is complete.

    Only allowed from BORRADOR or RECHAZADA. Returns 422 if mandatory checklist
    requisitos are still pending, listing them in the error detail.
    """
    # Routed through the shared tool registry (app/tools/catalog/cuentas.py) — same
    # invocation surface as the /mcp "radicar_cuenta" tool. No explicit commit here:
    # the tool wrapper (and the service it calls) only flush; the request's `get_db`
    # dependency commits after a successful response, same as before this swap.
    result = await invoke_tool("radicar_cuenta", ToolContext(db=db, usuario=user), {"cuenta_id": cuenta_id})
    return cast(CuentaCobroResponse, result)


@router.post("/{cuenta_id}/generar-pdf", response_model=GenerarPDFResponse)
async def generar_pdf(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> GenerarPDFResponse:
    """
    Generate a PDF for a CuentaCobro using the user's template (or the default one).
    Uploads the PDF to storage and returns a 1-hour presigned download URL.
    """
    return await cuenta_cobro_service.generar_pdf(db, user.id, cuenta_id, storage)


@router.get("/{cuenta_id}/pdf", response_model=PDFUrlResponse)
async def obtener_url_pdf(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    storage: S3StorageAdapter = Depends(get_pdf_storage),
) -> PDFUrlResponse:
    """Get a fresh 1-hour presigned URL for the stored PDF. Requires PDF to have been generated first."""
    return await cuenta_cobro_service.obtener_url_pdf(db, user.id, cuenta_id, storage)


@router.get("/{cuenta_id}/preview", response_class=HTMLResponse)
async def preview_cuenta_cobro(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Return the latest draft HTML preview for a CuentaCobro.

    The preview is generated by the AI agent (doc_assembly_node) and stored in
    ``borradores_cuenta_cobro.contenido["preview_html"]``.  This endpoint must be
    called before generating the final PDF — the PDF generation endpoint will check
    that a preview exists and has been implicitly approved by this call.

    Returns 404 if no draft exists yet (run the agent first).
    Returns 200 with Content-Type: text/html.
    """
    # Verify ownership via Contrato
    result = await db.execute(
        select(CuentaCobro)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            CuentaCobro.id == cuenta_id,
            Contrato.usuario_id == user.id,
        )
    )
    cuenta = result.scalar_one_or_none()
    if cuenta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CuentaCobro not found")

    # Fetch the latest approved draft, falling back to the latest unapproved draft
    borrador_result = await db.execute(
        select(BorradorCuentaCobro)
        .where(BorradorCuentaCobro.cuenta_cobro_id == cuenta_id)
        .order_by(BorradorCuentaCobro.version.desc())
        .limit(1)
    )
    borrador = borrador_result.scalar_one_or_none()

    if borrador is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No draft found. Run the AI agent to generate a preview first.",
        )

    # Extract HTML from draft content
    contenido = borrador.contenido or {}
    preview_html: str = (
        contenido.get("preview_html") or contenido.get("html") or _build_fallback_preview(cuenta, borrador)
    )

    return HTMLResponse(content=preview_html, status_code=200)


# ── Informes (DOCX/ZIP) downloads ────────────────────────────────────────────

_DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/{cuenta_id}/informe-actividades.docx", response_class=Response)
async def descargar_informe_actividades(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Generate and download the contractor's activities report as DOCX."""
    content, filename = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta_id)
    return Response(
        content=content,
        media_type=_DOCX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Machine-readable draft flag (task 7.5d) — avoids requiring callers to
            # parse the DOCX body just to detect the BORRADOR header.
            "X-Es-Borrador": str(informe_service.ES_BORRADOR).lower(),
        },
    )


@router.get("/{cuenta_id}/formato-valores", response_model=FormatoValoresResponse)
async def obtener_formato_valores(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> FormatoValoresResponse:
    """Preview the resolved campo values (contrato/contratista/cuota/computed)
    used to fill the organism's cloned templates."""
    valores, avisos = await informe_service.obtener_formato_valores(db, user.id, cuenta_id)
    return FormatoValoresResponse(valores=valores, avisos=avisos)


@router.get("/{cuenta_id}/cuenta-cobro.docx", response_class=Response)
async def descargar_cuenta_cobro_docx(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Generate and download the entity's cuenta-de-cobro letter as DOCX (clone-only)."""
    content, filename = await informe_service.generar_cuenta_cobro_docx(db, user.id, cuenta_id)
    return Response(
        content=content,
        media_type=_DOCX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Es-Borrador": str(informe_service.ES_BORRADOR).lower(),
        },
    )


@router.get("/{cuenta_id}/documento-soporte.xlsx", response_class=Response)
async def descargar_documento_soporte_xlsx(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Generate and download the entity's Documento Soporte as XLSX (clone-only, optional)."""
    content, filename = await informe_service.generar_documento_soporte_xlsx(db, user.id, cuenta_id)
    return Response(
        content=content,
        media_type=_XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Es-Borrador": str(informe_service.ES_BORRADOR).lower(),
        },
    )


@router.get("/{cuenta_id}/informe-supervision.docx", response_class=Response)
async def descargar_informe_supervision(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Generate and download the supervisor's report as DOCX."""
    content, filename = await informe_service.generar_informe_supervision_docx(db, user.id, cuenta_id)
    return Response(
        content=content,
        media_type=_DOCX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Es-Borrador": str(informe_service.ES_BORRADOR).lower(),
        },
    )


@router.get("/{cuenta_id}/evidencias.zip", response_class=Response)
async def descargar_zip_evidencias(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Download a ZIP folder structure (one folder per obligation) for evidence."""
    content, filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta_id)
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_fallback_preview(cuenta: CuentaCobro, borrador: BorradorCuentaCobro) -> str:
    """Build a minimal HTML preview from structured draft contenido."""
    contenido = borrador.contenido or {}
    lines = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>",
        f"<h1>Vista Previa — CuentaCobro #{cuenta.id}</h1>",
        f"<p><strong>Versión borrador:</strong> {borrador.version}</p>",
        f"<p><strong>Estado:</strong> {'Aprobado' if borrador.aprobado else 'Pendiente de aprobación'}</p>",
    ]
    for key, val in contenido.items():
        if isinstance(val, str):
            lines.append(f"<h2>{key}</h2><pre>{val[:3000]}</pre>")
    lines.append("</body></html>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Borradores diff endpoint
# ---------------------------------------------------------------------------


@router.get("/{cuenta_id}/borradores/diff")
async def get_borradores_diff(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    version_a: int = 1,
    version_b: int = 2,
) -> dict:
    """Return a diff between two draft versions of a CuentaCobro.

    Compares the ``contenido`` JSON of the two versions and returns a list of
    changed keys with their old and new values.  When the diff was precomputed
    by the agent it is returned directly from the ``diff`` column; otherwise it
    is computed on-the-fly by comparing JSON keys.
    """
    # Verify ownership
    cuenta_result = await db.execute(
        select(CuentaCobro)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(CuentaCobro.id == cuenta_id, Contrato.usuario_id == user.id)
    )
    if cuenta_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CuentaCobro not found")

    # Fetch the two requested versions
    result_a = await db.execute(
        select(BorradorCuentaCobro).where(
            BorradorCuentaCobro.cuenta_cobro_id == cuenta_id,
            BorradorCuentaCobro.version == version_a,
        )
    )
    result_b = await db.execute(
        select(BorradorCuentaCobro).where(
            BorradorCuentaCobro.cuenta_cobro_id == cuenta_id,
            BorradorCuentaCobro.version == version_b,
        )
    )
    borrador_a = result_a.scalar_one_or_none()
    borrador_b = result_b.scalar_one_or_none()

    if borrador_a is None or borrador_b is None:
        missing = []
        if borrador_a is None:
            missing.append(version_a)
        if borrador_b is None:
            missing.append(version_b)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Versions not found: {missing}",
        )

    # Use precomputed diff from version B if available
    if borrador_b.diff:
        return {
            "cuenta_cobro_id": str(cuenta_id),
            "version_a": version_a,
            "version_b": version_b,
            "diff": borrador_b.diff,
            "precomputed": True,
        }

    # Otherwise compute on-the-fly by comparing JSON keys
    content_a: dict = borrador_a.contenido or {}
    content_b: dict = borrador_b.contenido or {}
    all_keys = set(content_a.keys()) | set(content_b.keys())
    changes: list[dict] = []
    for key in sorted(all_keys):
        val_a = content_a.get(key)
        val_b = content_b.get(key)
        if val_a != val_b:
            changes.append({"key": key, "old": val_a, "new": val_b})

    return {
        "cuenta_cobro_id": str(cuenta_id),
        "version_a": version_a,
        "version_b": version_b,
        "diff": changes,
        "precomputed": False,
    }


@router.get("/{cuenta_id}/borradores", response_model=list[dict])
async def listar_borradores(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List all draft versions for a CuentaCobro, newest first."""
    # Verify ownership via Contrato
    cuenta_result = await db.execute(
        select(CuentaCobro)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            CuentaCobro.id == cuenta_id,
            Contrato.usuario_id == user.id,
        )
    )
    if cuenta_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="CuentaCobro not found")

    result = await db.execute(
        select(BorradorCuentaCobro)
        .where(BorradorCuentaCobro.cuenta_cobro_id == cuenta_id)
        .order_by(BorradorCuentaCobro.version.desc())
    )
    borradores = result.scalars().all()
    return [
        {
            "id": str(b.id),
            "version": b.version,
            "aprobado": b.aprobado,
            "feedback_usuario": b.feedback_usuario,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "has_diff": b.diff is not None,
        }
        for b in borradores
    ]
