"""Integraciones API — Google OAuth, Gmail, Drive."""

from __future__ import annotations

from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import DomainError
from app.schemas.google_workspace import (
    CalendarTestResponse,
    DriveTestResponse,
    DriveUploadRequest,
    DriveUploadResponse,
    EmailSearchRequest,
    EmailSearchResponse,
    EmailSendRequest,
    EmailSendResponse,
    EvidenceDiscoveryRequest,
    EvidenceDiscoveryResponse,
    GoogleConnectURLResponse,
    GoogleIntegrationStatus,
)
from app.services import evidence_discovery_service as eds
from app.services import google_workspace_service as gws

logger = structlog.get_logger("api.integraciones")
router = APIRouter(prefix="/integraciones", tags=["integraciones"])


# ── OAuth ────────────────────────────────────────────────────────────────────


@router.get("/google/connect", response_model=GoogleConnectURLResponse)
async def google_connect(user: CurrentUser) -> GoogleConnectURLResponse:
    """Genera la URL para que el usuario autorice el acceso a su cuenta de Google.

    Embeds a signed state token so the callback can recover the user without a JWT header.
    El frontend debe redirigir al usuario a `authorization_url`.
    """
    return gws.get_authorization_url(usuario_id=user.id)


@router.get("/google/callback")
async def google_oauth_callback(
    db: AsyncSession = Depends(get_db),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Callback de Google OAuth2. Google redirige el navegador aquí con el authorization code.

    No requiere JWT — la identidad del usuario viaja en el `state` firmado generado en /connect.
    Intercambia el código por tokens, los persiste y redirige el navegador de vuelta al frontend.
    """
    base = f"{settings.FRONTEND_URL}/integraciones"

    if error or not code or not state:
        reason = quote(error or "missing_code_or_state")
        logger.warning("google_oauth_callback_missing_params", error=error, has_code=bool(code))
        return RedirectResponse(f"{base}?google=error&reason={reason}", status_code=303)

    try:
        usuario_id, code_verifier = gws.verify_oauth_state(state)
        await gws.handle_oauth_callback(db=db, usuario_id=usuario_id, code=code, code_verifier=code_verifier)
    except DomainError as exc:
        logger.warning("google_oauth_callback_failed", detail=exc.detail)
        return RedirectResponse(f"{base}?google=error&reason={quote(exc.detail)}", status_code=303)

    return RedirectResponse(f"{base}?google=connected", status_code=303)


@router.get("/google/status", response_model=GoogleIntegrationStatus)
async def google_status(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> GoogleIntegrationStatus:
    """Retorna el estado de la integración Google del usuario autenticado."""
    return await gws.get_integration_status(db=db, usuario_id=user.id)


@router.delete("/google/revoke", status_code=200)
async def google_revoke(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Desconecta la cuenta de Google eliminando los tokens almacenados."""
    await gws.revoke_integration(db=db, usuario_id=user.id)
    return {"detail": "Integración de Google desconectada"}


# ── Evidence discovery (explorer agent) ──────────────────────────────────────


@router.post("/evidencias/descubrir", response_model=EvidenceDiscoveryResponse)
async def descubrir_evidencias(
    body: EvidenceDiscoveryRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EvidenceDiscoveryResponse:
    """Explora Gmail, Drive y Calendar del usuario para encontrar evidencias que justifiquen
    el cumplimiento de las obligaciones del período, y devuelve, por obligación, el texto de
    justificación más los links a las evidencias para montar la Cuenta de Cobro / Radicación.
    """
    return await eds.descubrir_evidencias(db=db, usuario_id=user.id, req=body)


# ── Gmail ────────────────────────────────────────────────────────────────────


@router.post("/email/search", response_model=EmailSearchResponse)
async def search_emails(
    body: EmailSearchRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EmailSearchResponse:
    """Busca correos en Gmail del usuario usando una query de Gmail.

    Ejemplos de query:
    - `subject:acta after:2025/01/01`
    - `from:supervisor@entidad.gov.co`
    - `subject:(informe OR entrega) after:2025/03/01 before:2025/04/01`
    """
    try:
        return await gws.search_emails(
            db=db,
            usuario_id=user.id,
            query=body.query,
            max_results=body.max_results,
        )
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/email/send", response_model=EmailSendResponse)
async def send_email(
    body: EmailSendRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> EmailSendResponse:
    """Envía un correo desde la cuenta Gmail del usuario.

    Si se provee `cuenta_cobro_id`, adjunta el PDF generado de esa cuenta de cobro.
    """
    pdf_bytes: bytes | None = None
    pdf_filename: str | None = None

    if body.cuenta_cobro_id:
        from sqlalchemy import select

        from app.adapters.storage.s3_adapter import S3StorageAdapter
        from app.core.config import settings
        from app.models.cuenta_cobro import CuentaCobro

        result = await db.execute(
            select(CuentaCobro).where(
                CuentaCobro.id == body.cuenta_cobro_id,
                CuentaCobro.usuario_id == user.id,  # type: ignore[attr-defined]
            )
        )
        cuenta = result.scalar_one_or_none()
        if cuenta and cuenta.pdf_storage_key:
            storage = S3StorageAdapter(bucket=settings.S3_BUCKET_PDFS)
            pdf_bytes = await storage.download(cuenta.pdf_storage_key)
            pdf_filename = f"cuenta_cobro_{cuenta.mes}_{cuenta.anio}.pdf"

    return await gws.send_invoice_email(
        db=db,
        usuario_id=user.id,
        to=[str(addr) for addr in body.to],
        subject=body.subject,
        body_html=body.body_html,
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_filename,
    )


# ── Drive ────────────────────────────────────────────────────────────────────


@router.get("/drive/test", response_model=DriveTestResponse)
async def test_drive(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    max_results: int = Query(default=10, ge=1, le=50),
) -> DriveTestResponse:
    """Lista los archivos más recientes del Drive del usuario (prueba de integración)."""
    from app.adapters.drive.drive_adapter import DriveAdapter
    from app.schemas.google_workspace import DriveFileTestItem

    try:
        adapter = DriveAdapter(db)
        files = await adapter.search_files(usuario_id=user.id, query="", max_results=max_results)
        items = [
            DriveFileTestItem(
                id=f.id,
                name=f.name,
                mime_type=f.mime_type,
                modified_at=f.modified_at,
                web_view_link=f.web_view_link,
            )
            for f in files
        ]
        return DriveTestResponse(files=items, total=len(items))
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.get("/calendar/test", response_model=CalendarTestResponse)
async def test_calendar(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    days: int = Query(default=60, ge=1, le=365),
) -> CalendarTestResponse:
    """Lista eventos de los últimos/próximos N días del Calendar del usuario (prueba de integración)."""
    from datetime import UTC, datetime, timedelta

    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter
    from app.schemas.google_workspace import CalendarEventItem

    now = datetime.now(UTC)
    time_min = (now - timedelta(days=days // 2)).isoformat()
    time_max = (now + timedelta(days=days // 2)).isoformat()

    try:
        adapter = GoogleCalendarAdapter(db)
        raw = await adapter.search_events(
            usuario_id=user.id,
            time_min=time_min,
            time_max=time_max,
            max_results=20,
        )

        def _extract_dt(boundary: dict) -> str:
            return boundary.get("dateTime") or boundary.get("date") or ""

        items = [
            CalendarEventItem(
                id=ev.get("id", ""),
                summary=ev.get("summary", "(sin título)"),
                start=_extract_dt(ev.get("start", {})),
                end=_extract_dt(ev.get("end", {})),
                location=ev.get("location"),
                html_link=ev.get("htmlLink"),
            )
            for ev in raw
        ]
        return CalendarTestResponse(events=items, total=len(items))
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc


@router.post("/drive/upload", response_model=DriveUploadResponse)
async def upload_to_drive(
    body: DriveUploadRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DriveUploadResponse:
    """Sube el PDF de una cuenta de cobro a Google Drive.

    Crea automáticamente la estructura de carpetas:
    `CashIn / {Entidad} / Contrato-{numero} / {año}-{mes}`
    """
    from sqlalchemy import select

    from app.adapters.storage.s3_adapter import S3StorageAdapter
    from app.core.config import settings
    from app.core.exceptions import NotFoundError, ValidationError
    from app.models.contrato import Contrato
    from app.models.cuenta_cobro import CuentaCobro

    # Cargar cuenta de cobro con su contrato
    result = await db.execute(
        select(CuentaCobro).where(
            CuentaCobro.id == body.cuenta_cobro_id,
        )
    )
    cuenta = result.scalar_one_or_none()
    if not cuenta:
        raise NotFoundError("CuentaCobro", str(body.cuenta_cobro_id))
    if not cuenta.pdf_storage_key:
        raise ValidationError("La cuenta de cobro no tiene PDF generado. Usa POST /generar-pdf primero.")

    result_c = await db.execute(select(Contrato).where(Contrato.id == cuenta.contrato_id))
    contrato = result_c.scalar_one_or_none()
    if not contrato:
        raise NotFoundError("Contrato")

    storage = S3StorageAdapter(bucket=settings.S3_BUCKET_PDFS)
    pdf_bytes = await storage.download(cuenta.pdf_storage_key)
    pdf_filename = f"cuenta_cobro_{cuenta.mes}_{cuenta.anio}.pdf"

    return await gws.upload_pdf_to_drive(
        db=db,
        usuario_id=user.id,
        pdf_bytes=pdf_bytes,
        filename=pdf_filename,
        entidad=contrato.entidad or "Sin Entidad",
        numero_contrato=contrato.numero_contrato,
        anio=cuenta.anio,
        mes=cuenta.mes,
        make_shareable=body.make_shareable,
    )
