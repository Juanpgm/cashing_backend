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
- Slice 4 persistence (tasks 4.2-4.4, FALLBACK PATH per task 4.1):
  `backend-local-first-sync` has not merged `_process_uploaded_document`/
  `DocumentoFuente.sha256` yet, so successfully scraped documents are
  persisted through the existing `document_service.upload_document(...)`
  entry point and its filename+tipo+contrato dedup. sha256 is computed here
  only for the `_document_index` skip-logic cache (task 4.4b), not enforced
  at the DB level. `upload_document`'s body is never modified.
- Task 4.4b: every SECOP DocumentId + RetrieveFile URL this module sees is
  cached (non-destructively) on `SecopContrato.datos_raw["_document_index"]`
  via `secop_service.upsert_document_index`. True fallback ordering
  (datasets → cached index → scraper): a DocumentId the scraper already
  found in a PREVIOUS manual trigger is captcha-free to refetch directly
  (`RetrieveFile` evidence, live-verified 2026-07-11) — a repeat trigger
  serves it from cache without spending scraper quota on Playwright again.

Known limitation (discovered during Slice 4, not fixed here — see tasks.md):
`document_service.upload_document` enforces "at most one tipo=CONTRATO
document per contrato" by deleting the previous one on each new upload.
Persisting more than one scraped doc per contract in the same slice means
only the LAST one survives. Flagged via a log warning + a `notas` entry
rather than silently swallowed, because it works against the "maximize
document acquisition" priority. Fixing it needs either a dedicated
scraped-doc `tipo` (would need a migration — out of scope, D1/rollout says
zero new migrations for this change) or `backend-local-first-sync`'s richer
classification.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.secop_scraper.dto import CaptchaRequiredError, ScrapedDocDTO, ScraperUnavailableError
from app.adapters.secop_scraper.port import SecopScraperPort
from app.agent.tools.multimodal_parser import guess_mime_type
from app.core.secop_agentic_quota import enforce_scraper_quota
from app.models.contrato import Contrato
from app.models.documento_fuente import TipoDocumentoFuente
from app.models.secop import SecopContrato
from app.schemas.secop import ScraperFallbackResult, SecopDocumentoResponse
from app.services import document_service, secop_service

log = structlog.get_logger("service.secop_scraper")

# Per-contract execution budget (spec: "~60-second execution budget per
# contract scrape"; design D-flow: `wait_for(60s)`).
_FETCH_TIMEOUT_S = 60

# Matches the existing upload endpoint's cap (`app/api/v1/documentos.py`) —
# scraped documents are subject to the same size discipline as user uploads.
_MAX_SCRAPED_DOC_SIZE_BYTES = 10 * 1024 * 1024

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
    contrato = await _find_contrato_usuario(db, user_id, numero_contrato)
    contrato_id = contrato.id if contrato else None

    # Task 4.4b: true fallback ordering — datasets (already tried upstream by
    # `obtener_documentos_con_cobertura` before a user reaches this manual
    # trigger) → cached DocumentId index → scraper.
    cached_result = await _intentar_desde_indice_cache(db, user_id, contrato_id, secop_contrato, numero_contrato)
    if cached_result is not None:
        return cached_result

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
    except Exception:
        # Fail-soft boundary: a non-enumerated adapter failure (malformed JSON
        # from a gateway, unexpected DTO shape, etc.) must degrade exactly like
        # an unavailable scraper — this manual trigger may never surface a 500.
        await log.aexception("secop_scraper_unexpected_error", numero_contrato=numero_contrato, notice_uid=notice_uid)
        return ScraperFallbackResult(
            estado="unavailable",
            documentos=[],
            notas="El servicio de scraping SECOP devolvió una respuesta inesperada.",
        )

    documentos, notas_list = await _persist_docs_and_update_index(
        db, user_id, contrato_id, secop_contrato, result.docs, numero_contrato, source="scraper"
    )

    if len(result.docs) > 1:
        await log.awarning(
            "secop_scraper_multi_doc_tipo_contrato_eviction_risk",
            numero_contrato=numero_contrato,
            count=len(result.docs),
        )
        notas_list.append(
            "Aviso: se encontraron varios documentos; por una limitación conocida del "
            "pipeline de persistencia (tipo=contrato admite solo 1 documento por "
            "contrato), solo el último persistido queda disponible. Ver tasks.md Slice 4."
        )

    return ScraperFallbackResult(estado="ok", documentos=documentos, notas="; ".join(notas_list) or None)


async def _intentar_desde_indice_cache(
    db: AsyncSession,
    user_id: uuid.UUID,
    contrato_id: uuid.UUID | None,
    secop_contrato: SecopContrato | None,
    numero_contrato: str,
) -> ScraperFallbackResult | None:
    """Task 4.4b cache tier: serve previously scraper-found DocumentIds from
    their cached (captcha-free) RetrieveFile URL, without invoking the
    scraper adapter. Returns ``None`` when there is nothing cached to serve —
    the caller then falls through to the real scraper invocation.

    Only entries tagged ``source="scraper"`` are eligible here — Socrata
    archive/modification entries are NOT treated as "the scraper already
    found this", since serving on any indexed DocumentId would make the
    manual trigger a no-op for the common case (a contract with ordinary
    Socrata-synced documents but no prior manual scrape).
    """
    if secop_contrato is None:
        return None
    index = (secop_contrato.datos_raw or {}).get("_document_index") or {}
    cached_dtos = [
        ScrapedDocDTO(
            document_id=doc_id,
            nombre_archivo=f"secop_{doc_id}",
            url_descarga=entry["url"],
            fecha_carga=None,
            extension=None,
            descripcion=None,
        )
        for doc_id, entry in index.items()
        if entry.get("source") == "scraper" and entry.get("url")
    ]
    if not cached_dtos:
        return None

    documentos, notas_list = await _persist_docs_and_update_index(
        db, user_id, contrato_id, secop_contrato, cached_dtos, numero_contrato, source="scraper"
    )
    await log.ainfo("secop_scraper_served_from_document_index", numero_contrato=numero_contrato, count=len(documentos))
    notas_list.append("Documentos servidos desde el índice cacheado (sin invocar el scraper).")
    return ScraperFallbackResult(estado="ok", documentos=documentos, notas="; ".join(notas_list) or None)


async def _persist_docs_and_update_index(
    db: AsyncSession,
    user_id: uuid.UUID,
    contrato_id: uuid.UUID | None,
    secop_contrato: SecopContrato | None,
    dtos: list[ScrapedDocDTO],
    numero_contrato: str,
    source: str,
) -> tuple[list[SecopDocumentoResponse], list[str]]:
    """Persist every DTO (task 4.2-4.4), then upsert the DocumentId index
    (task 4.4b) once, using a freshly-fetched `SecopContrato` row.

    A fresh re-fetch (rather than reusing the caller's in-memory
    ``secop_contrato``) is required because `document_service.upload_document`
    commits internally per document — with SQLAlchemy's default
    `expire_on_commit=True`, any ORM object tracked by the same session
    (including ``secop_contrato``) is expired after each of those commits, and
    a raw attribute read/write on an expired object outside an explicit
    awaited reload is unsafe under the async ORM.
    """
    # Read-only snapshot BEFORE any commits happen — used for the sha256
    # skip-logic check (task 4.3), safe because it is a plain dict copy.
    datos_raw_snapshot = dict((secop_contrato.datos_raw or {}) if secop_contrato else {})

    documentos: list[SecopDocumentoResponse] = []
    notas_list: list[str] = []
    index_entries: dict[str, dict[str, str | None]] = {}
    for dto in dtos:
        response, nota, sha256_hash = await _persistir_documento_scrapeado(
            db, user_id, contrato_id, datos_raw_snapshot, dto, numero_contrato
        )
        documentos.append(response)
        if nota:
            notas_list.append(nota)
        if dto.document_id:
            fields: dict[str, str | None] = {"url": dto.url_descarga, "source": source}
            if sha256_hash:
                fields["sha256"] = sha256_hash
            index_entries[dto.document_id] = fields

    if secop_contrato is not None and index_entries:
        fresh = await db.get(SecopContrato, secop_contrato.id)
        if fresh is not None:
            new_raw, changed = secop_service.upsert_document_index(fresh.datos_raw, index_entries)
            if changed:
                fresh.datos_raw = new_raw
                await db.commit()

    return documentos, notas_list


async def _persistir_documento_scrapeado(
    db: AsyncSession,
    user_id: uuid.UUID,
    contrato_id: uuid.UUID | None,
    datos_raw_snapshot: dict[str, Any],
    dto: ScrapedDocDTO,
    numero_contrato: str,
) -> tuple[SecopDocumentoResponse, str | None, str | None]:
    """Download + persist one scraped document. Returns the response row, an
    optional degradation `nota`, and the sha256 hash IF newly downloaded and
    persisted this call (``None`` when skipped, unpersisted, or failed).
    """
    response = _to_documento_response(dto, numero_contrato)

    if contrato_id is None:
        return (
            response,
            f"'{dto.nombre_archivo}' no se persistió: el contrato {numero_contrato} no está "
            "importado localmente (usa POST /secop/importar primero).",
            None,
        )
    if not dto.url_descarga:
        return response, f"'{dto.nombre_archivo}' no tiene URL de descarga disponible.", None

    # Skip-logic (task 4.3, FALLBACK PATH): a DocumentId already recorded with
    # a known hash in the document index was already downloaded+persisted by
    # a previous scrape — don't re-download bytes just to confirm what we
    # already know (no `DocumentoFuente.sha256` column exists yet to check
    # against directly; the index is the interim substitute per task 4.1).
    index = datos_raw_snapshot.get("_document_index") or {}
    cached_entry = index.get(dto.document_id) if dto.document_id else None
    if cached_entry and cached_entry.get("sha256"):
        return response, f"'{dto.nombre_archivo}' ya fue persistido previamente (mismo documento).", None

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            http_response = await client.get(dto.url_descarga)
            http_response.raise_for_status()
            content = http_response.content
    except Exception as exc:
        await log.awarning("secop_scraper_download_failed", nombre_archivo=dto.nombre_archivo, error=str(exc))
        return response, f"No se pudo descargar '{dto.nombre_archivo}': {exc}", None

    if len(content) > _MAX_SCRAPED_DOC_SIZE_BYTES:
        await log.awarning("secop_scraper_doc_too_large", nombre_archivo=dto.nombre_archivo, size=len(content))
        return response, f"'{dto.nombre_archivo}' excede el tamaño máximo permitido (10MB).", None

    sha256_hash = hashlib.sha256(content).hexdigest()

    try:
        upload_result = await document_service.upload_document(
            db,
            user_id,
            dto.nombre_archivo,
            content,
            guess_mime_type(dto.nombre_archivo),
            tipo=TipoDocumentoFuente.CONTRATO,
            contrato_id=contrato_id,
        )
    except Exception as exc:
        await log.aexception("secop_scraper_persist_failed", nombre_archivo=dto.nombre_archivo, error=str(exc))
        return response, f"No se pudo guardar '{dto.nombre_archivo}': {exc}", None

    response.id = upload_result.id
    return response, None, sha256_hash


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


async def _find_contrato_usuario(db: AsyncSession, user_id: uuid.UUID, numero_contrato: str) -> Contrato | None:
    """Resolve the requesting user's OWN `Contrato` row (not the SECOP cache
    row) — scraped documents persist against the user's contract, scoped so
    one user can never persist into another user's contract."""
    result = await db.execute(
        select(Contrato).where(
            Contrato.usuario_id == user_id,
            Contrato.numero_contrato == numero_contrato,
            Contrato.deleted_at.is_(None),
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
    """Build a response row from scraped doc metadata.

    `id` defaults to a synthetic UUID and is overwritten with the real
    `DocumentoFuente.id` when `_persistir_documento_scrapeado` successfully
    persists it; it stays synthetic for metadata-only/degraded outcomes
    (no local Contrato, download failure, size cap, already-persisted skip).
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
