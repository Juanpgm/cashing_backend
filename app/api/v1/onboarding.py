"""Onboarding API — guided onboarding flow via SECOP contract discovery."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import build_graph
from app.agent.state import AgentState
from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.agent import AgentMode

logger = structlog.get_logger("api.onboarding")

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# ── Request / Response schemas ──────────────────────────────────────────────


class SecopOnboardingRequest(BaseModel):
    cedula: str = Field(
        ...,
        pattern=r"^\d{5,15}$",
        description="Número de cédula del contratista (5-15 dígitos)",
        examples=["1016019452"],
    )


class SecopOnboardingResponse(BaseModel):
    session_id: uuid.UUID
    onboarding_mode: str = Field(description="'secop' si se encontraron contratos, 'manual' si no")
    contratos: list[dict] = Field(default_factory=list)
    documentos: list[dict] = Field(default_factory=list)
    message: str = Field(description="Respuesta en lenguaje natural para el usuario")


# ── Endpoint ────────────────────────────────────────────────────────────────


@router.post("/secop", response_model=SecopOnboardingResponse, status_code=200)
async def onboarding_secop(
    body: SecopOnboardingRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SecopOnboardingResponse:
    """Inicia el flujo de onboarding buscando los contratos del usuario en SECOP II.

    El agente busca contratos asociados a la cédula en las bases de datos
    de Socrata (SECOP II). Si encuentra contratos, retorna la lista para que
    el usuario seleccione con cuál trabajar. Si no, indica que debe ingresarlos
    manualmente.
    """
    session_id = uuid.uuid4()

    # Build a fresh no-checkpointer graph for this stateless endpoint call.
    # (Production uses the singleton _graph from agent_service; here we keep it
    # simple for onboarding which is a one-shot query, not a multi-turn chat.)
    graph = build_graph()

    initial_state: AgentState = {
        "session_id": session_id,
        "user_id": user.id,
        "mode": AgentMode.SECOP_DISCOVERY,
        "messages": [],
        "user_input": "__secop_onboarding__",
        "response": "",
        "cedula": body.cedula,
        "_db": db,
    }

    result = await graph.ainvoke(initial_state)

    contratos = result.get("secop_contratos") or []
    documentos = result.get("secop_documentos") or []
    mode = result.get("onboarding_mode") or "manual"
    message = result.get("response") or "Onboarding completado."

    await logger.ainfo(
        "onboarding.secop",
        user_id=str(user.id),
        cedula=body.cedula,
        n_contratos=len(contratos),
        mode=mode,
    )

    return SecopOnboardingResponse(
        session_id=session_id,
        onboarding_mode=mode,
        contratos=contratos,
        documentos=documentos,
        message=message,
    )


# ── Gmail / Drive first-load ─────────────────────────────────────────────────


class WorkspaceFirstLoadResponse(BaseModel):
    ok: bool
    items_found: int
    message: str


@router.post("/gmail-sync", response_model=WorkspaceFirstLoadResponse)
async def gmail_first_load(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceFirstLoadResponse:
    """Trigger a first-load sync of the user's Gmail inbox.

    Searches for emails matching common contract-related keywords and
    stores them as candidate evidence items linked to the user's contratos.
    Returns the number of emails found/indexed.
    """
    from app.services.google_workspace_service import GoogleWorkspaceService

    try:
        svc = GoogleWorkspaceService(db=db, usuario_id=user.id)
        results = await svc.search_emails(
            query="contrato OR cuenta OR cobro OR certificado OR informe",
            max_results=50,
        )
        count = len(results) if results else 0
        return WorkspaceFirstLoadResponse(
            ok=True,
            items_found=count,
            message=f"Gmail sincronizado: {count} correos encontrados como candidatos de evidencia.",
        )
    except Exception as exc:
        logger.warning("gmail_sync_failed", exc=str(exc))
        return WorkspaceFirstLoadResponse(
            ok=False,
            items_found=0,
            message="No se pudo conectar con Gmail. Verifica las credenciales de Google en Configuración.",
        )


@router.post("/drive-sync", response_model=WorkspaceFirstLoadResponse)
async def drive_first_load(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> WorkspaceFirstLoadResponse:
    """Trigger a first-load sync of the user's Google Drive.

    Searches for relevant documents (PDF, DOCX, spreadsheets) and indexes
    them as candidate evidence. Returns the number of files found.
    """
    from app.services.google_workspace_service import GoogleWorkspaceService

    try:
        svc = GoogleWorkspaceService(db=db, usuario_id=user.id)
        results = await svc.search_drive_files(
            query="contrato OR cuenta cobro OR informe actividades",
            max_results=30,
        )
        count = len(results) if results else 0
        return WorkspaceFirstLoadResponse(
            ok=True,
            items_found=count,
            message=f"Drive sincronizado: {count} archivos encontrados como candidatos de evidencia.",
        )
    except Exception as exc:
        logger.warning("drive_sync_failed", exc=str(exc))
        return WorkspaceFirstLoadResponse(
            ok=False,
            items_found=0,
            message="No se pudo conectar con Google Drive. Verifica las credenciales de Google en Configuración.",
        )

