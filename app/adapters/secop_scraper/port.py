"""Port (interface) for the SECOP II scraper.

Implementations:
- :class:`SecopScraperHttpAdapter` — calls the dedicated Playwright microservice.
- :class:`NullSecopScraperAdapter` — returns an empty list (used in tests / when
  the scraper service is not configured).
"""

from __future__ import annotations

from typing import Protocol

from app.adapters.secop_scraper.dto import ScrapeResult


class SecopScraperPort(Protocol):
    """Abstract contract for fetching SECOP II contract-phase documents."""

    async def fetch_contract_docs(
        self,
        notice_uid: str,
        ref_contrato: str | None = None,
    ) -> ScrapeResult:
        """Scrape document links for a contract.

        Parameters
        ----------
        notice_uid
            The SECOP II notice UID (e.g. ``CO1.NTC.9506401``).
        ref_contrato
            Internal reference for logging only (e.g. ``4161.010.26.1.155.2026``).

        Raises
        ------
        CaptchaRequiredError
            When the page is captcha-gated and the scraper cannot proceed.
        ScraperUnavailableError
            When the scraper microservice is unreachable or misconfigured.
        """
        ...
