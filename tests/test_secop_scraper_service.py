"""Tests for the SECOP II scraper orchestration service.

Slice 3 (no persistence) + Slice 4 (persistence via `document_service.upload_document`,
task 4.1 FALLBACK PATH — `backend-local-first-sync` has not merged, so there is no
`_process_uploaded_document`/`DocumentoFuente.sha256` to dedup against; sha256 is
computed here only for the `_document_index` skip-logic cache).

Fail-soft guarantee (secop-document-scraper spec): captcha, unavailable, and
timeout outcomes must NEVER raise to the caller — they are surfaced as
`ScraperFallbackResult.estado`. The ONLY exception path is the quota check
(`RateLimitExceededError`, mapped to HTTP 429 by the API layer).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
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
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente
from app.models.secop import SecopContrato, SecopDocumento
from app.models.usuario import Usuario
from app.services import document_service as ds
from app.services import secop_scraper_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_NUMERO = "CO1.PCCNTR.SCRAPER0001"
_NOTICE_URL = (
    "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
    "Index?noticeUID=CO1.NTC.9506401&isFromPublicArea=True"
)


class _FakeStorage:
    """Mirrors `tests/test_ocr_tier.py`'s in-memory storage fake — this
    codebase's established convention for bypassing real S3/MinIO in tests
    (no moto[s3] fixture currently exists in this suite to reuse)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        return key

    async def delete(self, key: str) -> None:
        return None


def _mock_httpx_download(monkeypatch: pytest.MonkeyPatch, content: bytes) -> AsyncMock:
    mock_response = MagicMock()
    mock_response.content = content
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client_cls)
    return mock_client


def _mock_httpx_download_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = AsyncMock()
    mock_client.get.side_effect = httpx.RequestError("connection refused")
    mock_client_cls = MagicMock()
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(httpx, "AsyncClient", mock_client_cls)


async def _seed_contrato_usuario(db: AsyncSession, user_id: uuid.UUID, numero: str = _NUMERO) -> Contrato:
    # Create the parent Usuario so contratos.usuario_id has a valid referent. SQLite
    # ignores the FK, but Postgres enforces it (contratos_usuario_id_fkey).
    db.add(
        Usuario(
            id=user_id,
            email=f"{user_id.hex}@t.com",
            nombre="Test",
            password_hash="x",
            rol="contratista",
            activo=True,
            creditos_disponibles=0,
        )
    )
    await db.flush()
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Objeto de prueba",
        valor_total=1_000_000,
        valor_mensual=100_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return contrato


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


class _ExplodingScraper:
    """Simulates a non-enumerated adapter failure (e.g. json.JSONDecodeError
    from a gateway error page) — must degrade fail-soft, never surface a 500."""

    async def fetch_contract_docs(self, notice_uid: str, ref_contrato: str | None = None) -> ScrapeResult:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


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


class _MultiDocScraper:
    """Returns >1 document in a single trigger — the DTO contract does not bound
    docs to 1, and `upload_document`'s 'one tipo=CONTRATO doc per contrato'
    replace-on-upload behavior would silently evict all but the last. The
    service must surface this known limitation (log warning + notas)."""

    async def fetch_contract_docs(self, notice_uid: str, ref_contrato: str | None = None) -> ScrapeResult:
        return ScrapeResult(
            docs=[
                ScrapedDocDTO(
                    document_id=f"70{n}",
                    nombre_archivo=f"Documento {n}.pdf",
                    url_descarga=f"https://x/RetrieveFile?DocumentId=70{n}",
                    fecha_carga=date(2026, 1, 5),
                    extension="pdf",
                    descripcion=f"Documento {n}",
                )
                for n in (1, 2)
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

    async def test_unexpected_adapter_error_is_fail_soft(self, db: AsyncSession) -> None:
        await _seed_contrato(db)
        result = await secop_scraper_service.explorar_documentos_agentico(
            db, _ExplodingScraper(), uuid.uuid4(), _NUMERO
        )
        assert result.estado == "unavailable"
        assert result.documentos == []
        assert result.notas

    async def test_success_returns_doc_metadata_no_persistence(self, db: AsyncSession) -> None:
        await _seed_contrato(db)
        result = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), uuid.uuid4(), _NUMERO)
        assert result.estado == "ok"
        assert len(result.documentos) == 1
        doc = result.documentos[0]
        assert doc.nombre_archivo == "Contrato Firmado.pdf"
        assert doc.numero_contrato == _NUMERO
        # No user Contrato seeded in this test → Slice 4 persistence is a no-op
        # (metadata-only, see TestPersistenciaScraper for the persisted path).
        # `secop_documentos` (the Socrata archive cache) is never touched by
        # the scraper fallback in any slice.
        persisted = (await db.execute(select(SecopDocumento))).scalars().all()
        assert persisted == []

    async def test_multi_doc_result_warns_eviction_risk(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Slice 4 verify WARNING: a >1-doc scraper result must surface the
        known tipo=CONTRATO eviction limitation (real data-loss risk) via both
        a structured log warning and a user-facing `notas` entry — not swallow it."""
        monkeypatch.setattr(ds, "_get_storage", _FakeStorage)
        monkeypatch.setattr(ds.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", False)
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        await _seed_contrato_usuario(db, user_id)
        _mock_httpx_download(monkeypatch, b"contenido binario de prueba")

        with patch.object(secop_scraper_service.log, "awarning", new_callable=AsyncMock) as mock_warn:
            result = await secop_scraper_service.explorar_documentos_agentico(db, _MultiDocScraper(), user_id, _NUMERO)

        assert result.estado == "ok"
        assert result.notas is not None
        warned_events = [call.args[0] for call in mock_warn.call_args_list if call.args]
        assert "secop_scraper_multi_doc_tipo_contrato_eviction_risk" in warned_events

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


@pytest.mark.asyncio
class TestPersistenciaScraper:
    """Slice 4 (tasks 4.2-4.4b): scraped documents persisted through the
    document ingestion pipeline (`document_service.upload_document` —
    FALLBACK PATH, task 4.1)."""

    async def test_new_document_persisted_via_upload_document(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ds, "_get_storage", _FakeStorage)
        monkeypatch.setattr(ds.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", False)
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        contrato = await _seed_contrato_usuario(db, user_id)
        _mock_httpx_download(monkeypatch, b"contenido binario de prueba")

        result = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)

        assert result.estado == "ok"
        persisted = (
            (await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id)))
            .scalars()
            .all()
        )
        assert len(persisted) == 1
        # get_safe_filename() sanitizes spaces (existing upload_document behavior).
        assert persisted[0].nombre == "Contrato_Firmado.pdf"
        assert result.documentos[0].id == persisted[0].id

    async def test_no_contrato_local_returns_metadata_only_with_nota(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-soft (task scope): the user has no matching Contrato imported
        locally — never crash, just report metadata + an explanatory nota."""
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        _mock_httpx_download(monkeypatch, b"contenido")

        result = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)

        assert result.estado == "ok"
        assert result.notas
        assert "no está importado" in result.notas
        persisted = (await db.execute(select(DocumentoFuente))).scalars().all()
        assert persisted == []

    async def test_download_failure_is_fail_soft_with_nota(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        await _seed_contrato_usuario(db, user_id)
        _mock_httpx_download_failure(monkeypatch)

        result = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)

        assert result.estado == "ok"
        assert result.notas
        assert "No se pudo descargar" in result.notas
        persisted = (await db.execute(select(DocumentoFuente))).scalars().all()
        assert persisted == []

    async def test_doc_exceeding_size_cap_skipped_with_nota(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ds, "_get_storage", _FakeStorage)
        monkeypatch.setattr(ds.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", False)
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        await _seed_contrato_usuario(db, user_id)
        _mock_httpx_download(monkeypatch, b"x" * (10 * 1024 * 1024 + 1))

        result = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)

        assert result.estado == "ok"
        assert result.notas
        assert "excede" in result.notas
        persisted = (await db.execute(select(DocumentoFuente))).scalars().all()
        assert persisted == []

    async def test_document_index_upsert_from_scraper_result(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 4.4b: every DocumentId + RetrieveFile URL the scraper returns
        lands in `SecopContrato.datos_raw["_document_index"]`, tagged
        `source="scraper"`, with the computed sha256 once persisted."""
        monkeypatch.setattr(ds, "_get_storage", _FakeStorage)
        monkeypatch.setattr(ds.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", False)
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        await _seed_contrato_usuario(db, user_id)
        _mock_httpx_download(monkeypatch, b"contenido")

        await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)

        refreshed = (
            await db.execute(select(SecopContrato).where(SecopContrato.numero_contrato == _NUMERO))
        ).scalar_one()
        index = refreshed.datos_raw.get("_document_index")
        assert index is not None
        assert index["999"]["url"] == "https://x/RetrieveFile?DocumentId=999"
        assert index["999"]["source"] == "scraper"
        assert index["999"]["sha256"]

    async def test_repeat_call_skips_redownload_via_hash_cache(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 4.3: a DocumentId already indexed with a sha256 (previously
        persisted) is not re-downloaded — no second copy, no wasted bytes."""
        monkeypatch.setattr(ds, "_get_storage", _FakeStorage)
        monkeypatch.setattr(ds.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", False)
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        await _seed_contrato_usuario(db, user_id)
        mock_client = _mock_httpx_download(monkeypatch, b"contenido")

        await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)
        assert mock_client.get.call_count == 1

        _reset()  # fresh hourly window — this test is about hash-skip, not quota
        result2 = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)

        assert mock_client.get.call_count == 1  # not re-downloaded
        persisted = (await db.execute(select(DocumentoFuente))).scalars().all()
        assert len(persisted) == 1  # no second copy
        assert result2.notas and "ya fue persistido" in result2.notas

    async def test_fallback_ordering_cached_url_hit_scraper_not_invoked(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task 4.4b: true fallback ordering (datasets → cached index →
        scraper) — once a DocumentId the scraper found is cached, a repeat
        manual trigger serves it from the cached RetrieveFile URL WITHOUT
        invoking the scraper adapter again."""
        monkeypatch.setattr(ds, "_get_storage", _FakeStorage)
        monkeypatch.setattr(ds.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", False)
        user_id = uuid.uuid4()
        await _seed_contrato(db)
        await _seed_contrato_usuario(db, user_id)
        _mock_httpx_download(monkeypatch, b"contenido")

        first = await secop_scraper_service.explorar_documentos_agentico(db, _OkScraper(), user_id, _NUMERO)
        assert first.estado == "ok"

        _reset()
        # Trap: a captcha-raising scraper would flip estado to "captcha_required"
        # if it were actually invoked — proves the cache short-circuited first.
        second = await secop_scraper_service.explorar_documentos_agentico(db, _CaptchaScraper(), user_id, _NUMERO)

        assert second.estado == "ok"
        assert len(second.documentos) == 1
