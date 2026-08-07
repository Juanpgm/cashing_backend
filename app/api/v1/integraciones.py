"""Integraciones API — Google OAuth, Gmail, Drive."""

from __future__ import annotations

from typing import cast
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

import app.tools.catalog  # noqa: F401 — import-for-side-effect: populates TOOL_REGISTRY
from app.api.deps import CurrentUser
from app.core.config import settings
from app.core.database import get_db
from app.core.exceptions import DomainError, NotFoundError, ValidationError
from app.models.integracion import IntegrationProvider
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
)
from app.schemas.integracion import IntegrationStatus
from app.services import google_workspace_service as gws
from app.services import integration_service
from app.services import microsoft_graph_service as mgs
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool

logger = structlog.get_logger("api.integraciones")
router = APIRouter(prefix="/integraciones", tags=["integraciones"])


# ── OAuth ────────────────────────────────────────────────────────────────────
#
# Routes generalized to a validated `{provider}` path parameter (design.md D1):
# `provider` is typed as `IntegrationProvider` (google|microsoft), so FastAPI
# rejects any other value with a 422 before it reaches a handler — a stray
# `/integraciones/evidencias/status` fails validation cleanly instead of being
# silently mis-routed. Existing `/integraciones/google/*` URLs keep resolving
# identically (provider=google), so no redirect-URI reconfiguration or frontend
# path change is required. Per-provider OAuth `Flow`/PKCE construction stays in
# each provider's own service module (D2); these routes are thin dispatchers.
#
# NOTE: `test_calendar`/`test_drive` (below) are intentionally NOT generalized
# in this slice — they require `MicrosoftGraphAdapter`, which is Slice C1 scope.


def _require_ms365_enabled(provider: IntegrationProvider) -> None:
    """Gate: the Microsoft branch of these routes stays 404 until
    `settings.MS365_INTEGRATION_ENABLED` is explicitly turned on (coded-but-not-active
    requirement — the code is fully unit-tested at the service/adapter/model layer
    regardless of this flag; only the live HTTP surface is gated). Google is unaffected.
    """
    if provider == IntegrationProvider.MICROSOFT and not settings.MS365_INTEGRATION_ENABLED:
        raise NotFoundError("Integración", provider.value)


@router.get("/{provider}/connect", response_model=GoogleConnectURLResponse)
async def integration_connect(provider: IntegrationProvider, user: CurrentUser) -> GoogleConnectURLResponse:
    """Genera la URL para que el usuario autorice el acceso a su cuenta (Google o Microsoft).

    Embeds a signed state token (carrying `provider`) so the callback can recover the
    user without a JWT header. El frontend debe redirigir al usuario a `authorization_url`.
    """
    _require_ms365_enabled(provider)
    if provider == IntegrationProvider.GOOGLE:
        return gws.get_authorization_url(usuario_id=user.id)
    return mgs.build_authorization_url(usuario_id=user.id)


@router.get("/{provider}/callback")
async def integration_callback(
    provider: IntegrationProvider,
    db: AsyncSession = Depends(get_db),
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Callback OAuth2. El proveedor redirige el navegador aquí con el authorization code.

    No requiere JWT — la identidad del usuario viaja en el `state` firmado generado en /connect.
    Intercambia el código por tokens, los persiste y redirige el navegador de vuelta al frontend.
    """
    _require_ms365_enabled(provider)
    base = f"{settings.FRONTEND_URL}/integraciones"
    label = provider.value

    if error or not code or not state:
        reason = quote(error or "missing_code_or_state")
        logger.warning("oauth_callback_missing_params", provider=label, error=error, has_code=bool(code))
        return RedirectResponse(f"{base}?{label}=error&reason={reason}", status_code=303)

    try:
        usuario_id, code_verifier, state_provider = integration_service.verify_oauth_state(state)
        if state_provider != provider:
            raise ValidationError("El estado OAuth no corresponde al proveedor solicitado")
        if provider == IntegrationProvider.GOOGLE:
            await gws.google_handle_oauth_callback(db=db, usuario_id=usuario_id, code=code, code_verifier=code_verifier)
        else:
            await mgs.handle_oauth_callback(db=db, usuario_id=usuario_id, code=code, code_verifier=code_verifier)
    except DomainError as exc:
        logger.warning("oauth_callback_failed", provider=label, detail=exc.detail)
        return RedirectResponse(f"{base}?{label}=error&reason={quote(exc.detail)}", status_code=303)

    return RedirectResponse(f"{base}?{label}=connected", status_code=303)


@router.get("/{provider}/status", response_model=IntegrationStatus)
async def integration_status(
    provider: IntegrationProvider,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> IntegrationStatus:
    """Retorna el estado de la integración (Google o Microsoft) del usuario autenticado.

    BREAKING CHANGE for existing Google consumers: this generalized route replaces
    the old `GoogleIntegrationStatus.gmail_enabled` field with `mail_enabled` (see
    `IntegrationStatus` docstring) — deliberate, requires frontend coordination.
    """
    _require_ms365_enabled(provider)
    return await integration_service.get_integration_status(db, user.id, provider)


@router.delete("/{provider}/revoke", status_code=200)
async def integration_revoke(
    provider: IntegrationProvider,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Desconecta la cuenta (Google o Microsoft) eliminando los tokens almacenados."""
    _require_ms365_enabled(provider)
    await integration_service.revoke_integration(db, user.id, provider)
    return {"detail": f"Integración de {provider.value} desconectada"}


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
    # Routed through the shared tool registry (app/tools/catalog/evidencias.py) — same
    # invocation surface as the /mcp "descubrir_evidencias" tool. Read-only, no credits
    # consumed here (consumes_credits is declarative metadata only, not yet enforced).
    result = await invoke_tool("descubrir_evidencias", ToolContext(db=db, usuario=user), body)
    return cast(EvidenceDiscoveryResponse, result)


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
    from app.adapters.drive.port import DriveQuery
    from app.schemas.google_workspace import DriveFileTestItem

    try:
        adapter = DriveAdapter(db)
        files = await adapter.search_files(usuario_id=user.id, query=DriveQuery(keywords=[], max_results=max_results))
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
    from datetime import UTC, date, datetime, timedelta

    from app.adapters.calendar.calendar_adapter import GoogleCalendarAdapter
    from app.schemas.google_workspace import CalendarEventItem

    now = datetime.now(UTC)
    time_min = (now - timedelta(days=days // 2)).isoformat()
    time_max = (now + timedelta(days=days // 2)).isoformat()

    try:
        adapter = GoogleCalendarAdapter(db)
        events = await adapter.search_events(
            usuario_id=user.id,
            time_min=time_min,
            time_max=time_max,
            max_results=20,
        )

        def _extract_dt(dt: datetime | None, d: date | None) -> str:
            if dt is not None:
                return dt.isoformat()
            if d is not None:
                return d.isoformat()
            return ""

        items = [
            CalendarEventItem(
                id=ev.id,
                summary=ev.summary or "(sin título)",
                start=_extract_dt(ev.start, ev.start_date),
                end=_extract_dt(ev.end, ev.end_date),
                location=ev.location,
                html_link=ev.html_link,
            )
            for ev in events
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
