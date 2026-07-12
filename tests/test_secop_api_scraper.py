"""Integration tests for `POST /secop/explorar-agentico` (Slice 3).

Covers the spec scenarios:
- Flag off → no-op, non-erroring 200 response (null adapter).
- Quota exceeded → 429 before invoking the adapter.
- Captcha → 200 body with `estado="captcha_required"` (never a 5xx/exception).
"""

from __future__ import annotations

from typing import Any

import pytest
from app.adapters.secop_scraper.dto import CaptchaRequiredError, ScrapeResult
from app.api.deps import get_secop_scraper
from app.core.config import settings
from app.core.secop_agentic_quota import _reset
from app.main import app as fastapi_app
from app.models.secop import SecopContrato
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_NUMERO = "CO1.PCCNTR.APISCRAPER01"
_NOTICE_URL = (
    "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
    "Index?noticeUID=CO1.NTC.9506401&isFromPublicArea=True"
)


@pytest.fixture(autouse=True)
def _clean_quota() -> None:
    _reset()


@pytest.fixture(autouse=True)
def _clean_overrides() -> None:
    yield
    fastapi_app.dependency_overrides.pop(get_secop_scraper, None)


async def _seed_contrato(db: AsyncSession) -> None:
    db.add(
        SecopContrato(
            id_contrato_secop=f"SECOP-{_NUMERO}",
            cedula_contratista="123456789",
            numero_contrato=_NUMERO,
            datos_raw={"urlproceso": _NOTICE_URL},
        )
    )
    await db.commit()


class _CaptchaScraper:
    async def fetch_contract_docs(self, notice_uid: str, ref_contrato: str | None = None) -> ScrapeResult:
        raise CaptchaRequiredError(manual_action_url="https://manual/captcha")


class TestExplorarAgenticoEndpoint:
    async def test_flag_off_returns_noop_ok(
        self, client: AsyncClient, test_user: dict[str, Any], db: AsyncSession
    ) -> None:
        await _seed_contrato(db)
        resp = await client.post(
            "/api/v1/secop/explorar-agentico",
            params={"numero_contrato": _NUMERO},
            headers=test_user["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["estado"] == "ok"
        assert body["documentos"] == []

    async def test_quota_exceeded_returns_429(
        self,
        client: AsyncClient,
        test_user: dict[str, Any],
        db: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "SECOP_SCRAPER_HOURLY_LIMIT", 1, raising=False)
        await _seed_contrato(db)
        first = await client.post(
            "/api/v1/secop/explorar-agentico",
            params={"numero_contrato": _NUMERO},
            headers=test_user["headers"],
        )
        assert first.status_code == 200, first.text
        second = await client.post(
            "/api/v1/secop/explorar-agentico",
            params={"numero_contrato": _NUMERO},
            headers=test_user["headers"],
        )
        assert second.status_code == 429

    async def test_captcha_maps_to_200_body(
        self, client: AsyncClient, test_user: dict[str, Any], db: AsyncSession
    ) -> None:
        await _seed_contrato(db)
        fastapi_app.dependency_overrides[get_secop_scraper] = lambda: _CaptchaScraper()
        resp = await client.post(
            "/api/v1/secop/explorar-agentico",
            params={"numero_contrato": _NUMERO},
            headers=test_user["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["estado"] == "captcha_required"
        assert body["manual_action_url"] == "https://manual/captcha"
