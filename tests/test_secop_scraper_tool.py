"""Tests for the `explorar_documentos_secop_agentico` tool registration (Slice 3, task 3.12).

Reconciled tool name (see tasks.md "Reconciliation: Spec vs Design"): the
spec originally proposed `explorar_documentos_plataforma_secop`; design's
final name is `explorar_documentos_secop_agentico`, matching the existing
"agentic" naming convention (`secop_agentic_quota.py`,
`SECOP_AGENTIC_HOURLY_LIMIT`, `POST /secop/explorar-agentico`).
"""

from __future__ import annotations

import pytest
from app.adapters.secop_scraper.dto import CaptchaRequiredError, ScrapeResult
from app.core.secop_agentic_quota import _reset
from app.models.secop import SecopContrato
from app.tools.context import ToolContext
from sqlalchemy.ext.asyncio import AsyncSession

_NUMERO = "CO1.PCCNTR.TOOLSCRAPER01"
_NOTICE_URL = (
    "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
    "Index?noticeUID=CO1.NTC.9506401&isFromPublicArea=True"
)


@pytest.fixture(autouse=True)
def _clean_quota() -> None:
    _reset()


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


class TestExplorarDocumentosSecopAgenticoToolRegistration:
    def test_tool_is_registered_as_write(self) -> None:
        import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
        from app.tools.registry import TOOL_REGISTRY

        assert "explorar_documentos_secop_agentico" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["explorar_documentos_secop_agentico"]
        assert spec.tags == ("write",)


@pytest.mark.asyncio
class TestExplorarDocumentosSecopAgenticoInvocation:
    async def test_invocation_flag_off_returns_ok_empty(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.tools.catalog  # noqa: F401
        from app.core.security import hash_password
        from app.models.usuario import Usuario
        from app.schemas.secop import ScraperFallbackResult
        from app.tools.invoke import invoke_tool

        await _seed_contrato(db)
        user = Usuario(
            email="tool-scraper@example.com",
            nombre="Tool Scraper",
            cedula="999999999",
            telefono="+573000000000",
            password_hash=hash_password("Pass123!"),
            rol="contratista",
            activo=True,
            creditos_disponibles=100,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        ctx = ToolContext(db=db, usuario=user)
        result = await invoke_tool("explorar_documentos_secop_agentico", ctx, {"numero_contrato": _NUMERO})
        assert isinstance(result, ScraperFallbackResult)
        assert result.estado == "ok"
        assert result.documentos == []

    async def test_invocation_captcha_maps_through_tool(
        self, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.tools.catalog  # noqa: F401
        import app.tools.catalog.secop as secop_catalog
        from app.core.security import hash_password
        from app.models.usuario import Usuario
        from app.tools.invoke import invoke_tool

        await _seed_contrato(db)
        monkeypatch.setattr(secop_catalog, "get_secop_scraper_gated", lambda: _CaptchaScraper())

        user = Usuario(
            email="tool-scraper-captcha@example.com",
            nombre="Tool Scraper Captcha",
            cedula="999999998",
            telefono="+573000000001",
            password_hash=hash_password("Pass123!"),
            rol="contratista",
            activo=True,
            creditos_disponibles=100,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

        ctx = ToolContext(db=db, usuario=user)
        result = await invoke_tool("explorar_documentos_secop_agentico", ctx, {"numero_contrato": _NUMERO})
        assert result.estado == "captcha_required"
        assert result.manual_action_url == "https://manual/captcha"
