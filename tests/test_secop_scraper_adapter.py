"""Unit tests for the SECOP II scraper adapter (port + http + null + factory)."""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from app.adapters.secop_scraper import (
    CaptchaRequiredError,
    NullSecopScraperAdapter,
    ScraperUnavailableError,
    SecopScraperHttpAdapter,
    get_secop_scraper,
)


class TestNullAdapter:
    @pytest.mark.asyncio
    async def test_returns_empty_result(self) -> None:
        adapter = NullSecopScraperAdapter()
        res = await adapter.fetch_contract_docs("CO1.NTC.999")
        assert res.docs == []
        assert res.duration_ms == 0
        assert res.captcha_solved is False


class TestHttpAdapterValidation:
    def test_missing_url_raises(self) -> None:
        with pytest.raises(ScraperUnavailableError):
            SecopScraperHttpAdapter(base_url="", internal_token="token")

    def test_missing_token_raises(self) -> None:
        with pytest.raises(ScraperUnavailableError):
            SecopScraperHttpAdapter(base_url="http://x", internal_token="")


def _build_adapter() -> SecopScraperHttpAdapter:
    return SecopScraperHttpAdapter(
        base_url="http://scraper.test",
        internal_token="secret",
        timeout=httpx.Timeout(5.0),
    )


class TestHttpAdapterCalls:
    @pytest.mark.asyncio
    async def test_success_returns_dtos(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _build_adapter()

        async def fake_post(self, url, json=None, headers=None):  # noqa: ANN001
            assert url == "http://scraper.test/scrape/contract-docs"
            assert headers["X-Internal-Token"] == "secret"
            assert json["notice_uid"] == "CO1.NTC.1"
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "document_id": "777",
                            "nombre_archivo": "Acta.pdf",
                            "url_descarga": "https://x/RetrieveFile?DocumentId=777",
                            "fecha_carga": "2025-01-15",
                            "extension": "pdf",
                            "descripcion": "Acta firmada",
                        }
                    ],
                    "duration_ms": 4321,
                    "captcha_solved": False,
                    "notice_uid": "CO1.NTC.1",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        res = await adapter.fetch_contract_docs("CO1.NTC.1", "ref-1")
        assert res.duration_ms == 4321
        assert len(res.docs) == 1
        assert res.docs[0].document_id == "777"
        assert res.docs[0].fecha_carga == date(2025, 1, 15)
        assert res.docs[0].extension == "pdf"

    @pytest.mark.asyncio
    async def test_503_raises_captcha_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _build_adapter()

        async def fake_post(self, url, json=None, headers=None):  # noqa: ANN001
            return httpx.Response(
                503,
                json={
                    "detail": {
                        "error": "captcha_required",
                        "manual_action_url": "https://manual",
                        "message": "captcha please",
                    }
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(CaptchaRequiredError) as exc_info:
            await adapter.fetch_contract_docs("CO1.NTC.1")
        assert exc_info.value.manual_action_url == "https://manual"

    @pytest.mark.asyncio
    async def test_5xx_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter = _build_adapter()

        async def fake_post(self, url, json=None, headers=None):  # noqa: ANN001
            return httpx.Response(502, text="boom", request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(ScraperUnavailableError):
            await adapter.fetch_contract_docs("CO1.NTC.1")

    @pytest.mark.asyncio
    async def test_network_error_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter = _build_adapter()

        async def fake_post(self, url, json=None, headers=None):  # noqa: ANN001
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(ScraperUnavailableError):
            await adapter.fetch_contract_docs("CO1.NTC.1")


class TestFactory:
    def test_returns_null_when_unconfigured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "SECOP_SCRAPER_URL", "", raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_INTERNAL_TOKEN", "", raising=False)
        adapter = get_secop_scraper()
        assert isinstance(adapter, NullSecopScraperAdapter)

    def test_returns_http_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import settings

        monkeypatch.setattr(settings, "SECOP_SCRAPER_URL", "http://x", raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_INTERNAL_TOKEN", "tok", raising=False)
        adapter = get_secop_scraper()
        assert isinstance(adapter, SecopScraperHttpAdapter)
