"""HTTP adapter that proxies calls to the SECOP II scraper microservice."""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import structlog

from app.adapters.secop_scraper.dto import (
    CaptchaRequiredError,
    ScrapedDocDTO,
    ScrapeResult,
    ScraperUnavailableError,
)

log = structlog.get_logger("secop_scraper.http_adapter")

# We allow a generous timeout because Playwright navigation + wait can be slow.
_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=10.0)


class SecopScraperHttpAdapter:
    """Calls POST /scrape/contract-docs on the scraper microservice."""

    def __init__(self, base_url: str, internal_token: str, timeout: httpx.Timeout | None = None) -> None:
        if not base_url:
            raise ScraperUnavailableError("SECOP_SCRAPER_URL is not configured")
        if not internal_token:
            raise ScraperUnavailableError("SECOP_SCRAPER_INTERNAL_TOKEN is not configured")
        self._base_url = base_url.rstrip("/")
        self._token = internal_token
        self._timeout = timeout or _DEFAULT_TIMEOUT

    async def fetch_contract_docs(
        self,
        notice_uid: str,
        ref_contrato: str | None = None,
    ) -> ScrapeResult:
        url = f"{self._base_url}/scrape/contract-docs"
        payload = {"notice_uid": notice_uid, "ref_contrato": ref_contrato}
        headers = {"X-Internal-Token": self._token}

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            await log.aerror("scraper.http_failed", error=str(exc), notice_uid=notice_uid)
            raise ScraperUnavailableError(f"Cannot reach scraper service: {exc}") from exc

        if resp.status_code == 503:
            try:
                detail = resp.json().get("detail") or {}
            except Exception:
                detail = {}
            raise CaptchaRequiredError(
                manual_action_url=detail.get("manual_action_url")
                or (
                    "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
                    f"Index?noticeUID={notice_uid}&isFromPublicArea=True"
                ),
                message=detail.get("message") or "Captcha required by SECOP II",
            )
        if resp.status_code >= 400:
            raise ScraperUnavailableError(
                f"Scraper service returned {resp.status_code}: {resp.text[:200]}"
            )

        data = resp.json()
        docs = [_to_dto(d) for d in data.get("docs", [])]
        return ScrapeResult(
            docs=docs,
            duration_ms=int(data.get("duration_ms", 0)),
            captcha_solved=bool(data.get("captcha_solved", False)),
        )


def _to_dto(d: dict[str, Any]) -> ScrapedDocDTO:
    fecha_raw = d.get("fecha_carga")
    fecha: date | None = None
    if fecha_raw:
        try:
            fecha = date.fromisoformat(fecha_raw)
        except (TypeError, ValueError):
            fecha = None
    return ScrapedDocDTO(
        document_id=d.get("document_id"),
        nombre_archivo=d.get("nombre_archivo") or "documento",
        url_descarga=d.get("url_descarga") or "",
        fecha_carga=fecha,
        extension=d.get("extension"),
        descripcion=d.get("descripcion"),
        tipo_origen=d.get("tipo_origen") or "contrato_firmado",
    )
