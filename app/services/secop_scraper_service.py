"""Orchestrates the SECOP II scraper as a manual, fail-soft fallback.

Wires the already-built Playwright scraper adapter (`SecopScraperPort`) end
to end (secop-document-scraper spec, design D6/D7):

- Manual trigger only — never called from `sincronizar_documentos_secop` or
  checklist evaluation.
- Quota-gated (`enforce_scraper_quota`) — the ONLY path that raises to the
  caller (`RateLimitExceededError` → HTTP 429). Every other failure mode
  (captcha, unavailable, timeout, missing notice_uid) is fail-soft: it
  returns a `ScraperFallbackResult` with a descriptive `estado`, never an
  exception.
- No persistence in this slice (task 3.14) — successfully scraped documents
  are returned as metadata only. Slice 4 wires persistence through the
  document ingestion pipeline (`document_service.upload_document`).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.secop_scraper.dto import CaptchaRequiredError, ScrapedDocDTO, ScraperUnavailableError
from app.adapters.secop_scraper.port import SecopScraperPort
from app.core.secop_agentic_quota import enforce_scraper_quota
from app.models.secop import SecopContrato
from app.schemas.secop import ScraperFallbackResult, SecopDocumentoResponse

log = structlog.get_logger("service.secop_scraper")

# Per-contract execution budget (spec: "~60-second execution budget per
# contract scrape"; design D-flow: `wait_for(60s)`).
_FETCH_TIMEOUT_S = 60

_NO_NOTICE_UID_NOTA = (
    "No se pudo determinar el aviso SECOP (notice_uid) para este contrato. "
    "Sincroniza el contrato primero (GET /secop/contratos o POST /secop/importar) "
    "para que quede disponible su urlproceso."
)


async def explorar_documentos_agentico(
    db: AsyncSession,
    scraper: SecopScraperPort,
    user_id: uuid.UUID,
    numero_contrato: str,
) -> ScraperFallbackResult:
    """Manually trigger the scraper fallback for one contract's documents.

    Enforces the per-user hourly scraper quota BEFORE touching the adapter
    (`RateLimitExceededError` propagates to the caller — this is the only
    exception path). Every scraper-side failure (captcha, unavailable,
    timeout) degrades to a `ScraperFallbackResult.estado`, never raises.
    """
    enforce_scraper_quota(str(user_id))

    secop_contrato = await _find_secop_contrato(db, numero_contrato)
    notice_uid = _derive_notice_uid(secop_contrato)
    if not notice_uid:
        await log.awarning("secop_scraper_no_notice_uid", numero_contrato=numero_contrato, usuario_id=str(user_id))
        return ScraperFallbackResult(estado="unavailable", documentos=[], notas=_NO_NOTICE_UID_NOTA)

    try:
        result = await asyncio.wait_for(
            scraper.fetch_contract_docs(notice_uid, numero_contrato), timeout=_FETCH_TIMEOUT_S
        )
    except CaptchaRequiredError as exc:
        await log.awarning("secop_scraper_captcha_required", numero_contrato=numero_contrato, notice_uid=notice_uid)
        return ScraperFallbackResult(
            estado="captcha_required",
            documentos=[],
            manual_action_url=exc.manual_action_url,
            notas=str(exc),
        )
    except (ScraperUnavailableError, TimeoutError):
        await log.awarning("secop_scraper_unavailable", numero_contrato=numero_contrato, notice_uid=notice_uid)
        return ScraperFallbackResult(
            estado="unavailable",
            documentos=[],
            notas="El servicio de scraping SECOP no está disponible en este momento.",
        )

    documentos = [_to_documento_response(dto, numero_contrato) for dto in result.docs]
    return ScraperFallbackResult(estado="ok", documentos=documentos)


async def _find_secop_contrato(db: AsyncSession, numero_contrato: str) -> SecopContrato | None:
    result = await db.execute(
        select(SecopContrato).where(
            or_(
                SecopContrato.numero_contrato == numero_contrato,
                SecopContrato.referencia_del_contrato == numero_contrato,
                SecopContrato.proceso_de_compra == numero_contrato,
            )
        )
    )
    return result.scalars().first()


def _derive_notice_uid(secop_contrato: SecopContrato | None) -> str | None:
    """Extract the SECOP II notice UID (e.g. `CO1.NTC.9506401`) from the
    cached contract's `urlproceso` query string (`?noticeUID=...`)."""
    if secop_contrato is None:
        return None
    raw = secop_contrato.datos_raw or {}
    url_val = raw.get("urlproceso")
    if isinstance(url_val, dict):
        url_val = url_val.get("url")
    if not url_val or not isinstance(url_val, str):
        return None
    values = parse_qs(urlparse(url_val).query).get("noticeUID")
    return values[0] if values else None


def _to_documento_response(dto: ScrapedDocDTO, numero_contrato: str) -> SecopDocumentoResponse:
    """Build a transient (non-persisted) response row from scraped doc metadata.

    Task 3.14 explicitly stubs persistence this slice — the `id` is synthetic
    and is NOT a real `secop_documentos` primary key until Slice 4 wires
    persistence via the document ingestion pipeline.
    """
    return SecopDocumentoResponse(
        id=uuid.uuid4(),
        id_documento_secop=dto.document_id or "",
        numero_contrato=numero_contrato,
        proceso=None,
        secop_contrato_id=None,
        secop_proceso_id=None,
        nombre_archivo=dto.nombre_archivo,
        extension=dto.extension,
        descripcion=dto.descripcion,
        fecha_carga=dto.fecha_carga,
        entidad=None,
        nit_entidad=None,
        url_descarga=dto.url_descarga,
        tipo_origen=dto.tipo_origen,
        updated_at=datetime.now(UTC),
    )
