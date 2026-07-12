"""Tests for the SECOP II scraper orchestration service (Slice 3, no persistence).

Fail-soft guarantee (secop-document-scraper spec): captcha, unavailable, and
timeout outcomes must NEVER raise to the caller — they are surfaced as
`ScraperFallbackResult.estado`. The ONLY exception path is the quota check
(`RateLimitExceededError`, mapped to HTTP 429 by the API layer).
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from app.adapters.secop_scraper.dto import (
    CaptchaRequiredError,
    ScrapedDocDTO,
    ScrapeResult,
    ScraperUnavailableError,
)
from app.core.config import settings
from app.core.exceptions import RateLimitExceededError
from app.core.secop_agentic_quota import _reset
from app.models.secop import SecopContrato, SecopDocumento
from app.services import secop_scraper_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_NUMERO = "CO1.PCCNTR.SCRAPER0001"
_NOTICE_URL = (
    "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
    "Index?noticeUID=CO1.NTC.9506401&isFromPublicArea=True"
)


@pytest.fixture(autouse=True)
def _clean_quota() -> None:
    _reset()


async def _seed_contrato(db: AsyncSession, *, numero: str = _NUMERO, urlproceso: str | None = _NOTICE_URL) -> None:
    db.add(
        SecopContrato(
            id_contrato_secop=f"SECOP-{numero}",
            cedula_contratista="123456789",
            numero_contrato=numero,
            datos_raw={"urlproceso": urlproceso} if urlproceso else {},
        )
    )
    await db.commit()


class _CaptchaScraper:
    async def fetch_contract_docs(self, notice_uid: str, ref_contrato: str | None = None) -> ScrapeResult:
        raise CaptchaRequiredError(manual_action_url="https://manual/captcha")


class _UnavailableScraper:
    async def fetch_contract_docs(self, notice_uid: str, ref_contrato: str | None = None) -> ScrapeResult:
        raise ScraperUnavailableError("scraper microservice unreachable")


class _TimeoutScraper:
    async def fetch_contract_docs(self, notice_uid: str, ref_contrato: str | None = None) -> ScrapeResult:
        raise TimeoutError()


class _OkScraper:
    async def fetch_contract_docs(self, notice_uid: str, ref_contrato: str | None = None) -> ScrapeResult:
        return ScrapeResult(
            docs=[
                ScrapedDocDTO(
                    document_id="999",
                    nombre_archivo="Contrato Firmado.pdf",
                    url_descarga="https://x/RetrieveFile?DocumentId=999",
                    fecha_carga=date(2026, 1, 5),
                    extension="pdf",
                    descripcion="Contrato firmado",
                )
            ],
            duration_ms=1234,
            captcha_solved=False,
        )


@pytest.mark.asyncio
class TestExplorarDocumentosAgentico:
    async def test_captcha_returns_captcha_required_state(self, db: AsyncSession) -> None:
        await _seed_contrato(db)
        result = await secop_scraper_service.explorar_documentos_agentico(db, _CaptchaScraper(), uuid.uuid4(), _NUMERO)
        assert result.estado == "captcha_required"
        assert result.manual_action_url == "https://manual/captcha"
        assert result.documentos == []

    async def test_unavailable_is_fail_soft(self, db: AsyncSession) -> None:
        await _seed_contrato(db)
        result = await secop_scraper_service.explorar_documentos_agentico(
            db, _UnavailableScraper(), uuid.uuid4(), _NUMERO
        )
        assert result.estado == "unavailable"
        assert result.documentos == []

    async def test_timeout_is_fail_soft(self, db: AsyncSession) -> None:
        await _seed_contrato(db)
        result = await secop_scraper_service.explorar_documentos_agentico(db, _TimeoutScraper(), uuid.uuid4(), _NUMERO)
        assert result.estado == "unavailable"

    async def test_missing_notice_uid_is_unavailable_not_exception(self, db: AsyncSession) -> None:
        await _seed_contrato(db, numero="CO1.PCCNTR.NOURL", urlproceso=None)
        result = await secop_scraper_service.explorar_documentos_agentico(
            db, _OkScraper(), uuid.uuid4(), "CO1.PCCNTR.NOURL"
        )
        assert result.estado == "unavailable"
        assert result.notas

    async def test_success_returns_doc_metadata_no_persistence(self, db: AsyncSession) -> None:
        await _seed_contrato(db)
        result = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), uuid.uuid4(), _NUMERO)
        assert result.estado == "ok"
        assert len(result.documentos) == 1
        doc = result.documentos[0]
        assert doc.nombre_archivo == "Contrato Firmado.pdf"
        assert doc.numero_contrato == _NUMERO
        # Task 3.14: no persistence this slice — nothing written to secop_documentos.
        persisted = (await db.execute(select(SecopDocumento))).scalars().all()
        assert persisted == []

    async def test_quota_exceeded_raises_before_invoking_adapter(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "SECOP_SCRAPER_HOURLY_LIMIT", 1, raising=False)
        await _seed_contrato(db)
        user_id = uuid.uuid4()
        await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)
        with pytest.raises(RateLimitExceededError):
            # Uses the captcha scraper to prove the adapter is never invoked —
            # if quota didn't short-circuit first we'd see estado="captcha_required".
            await secop_scraper_service.explorar_documentos_agentico(db, _CaptchaScraper(), user_id, _NUMERO)
