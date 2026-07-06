"""Factory and re-exports for the SECOP II scraper adapter."""

from __future__ import annotations

from app.adapters.secop_scraper.dto import (
    CaptchaRequiredError,
    ScrapedDocDTO,
    ScraperUnavailableError,
    ScrapeResult,
)
from app.adapters.secop_scraper.http_adapter import SecopScraperHttpAdapter
from app.adapters.secop_scraper.null_adapter import NullSecopScraperAdapter
from app.adapters.secop_scraper.port import SecopScraperPort
from app.core.config import settings


def get_secop_scraper() -> SecopScraperPort:
    """Return the configured scraper adapter.

    If ``SECOP_SCRAPER_URL`` and ``SECOP_SCRAPER_INTERNAL_TOKEN`` are set, returns
    the HTTP adapter. Otherwise returns the null adapter (agentic mode falls
    back to normal results).
    """
    url = getattr(settings, "SECOP_SCRAPER_URL", "") or ""
    token = getattr(settings, "SECOP_SCRAPER_INTERNAL_TOKEN", "") or ""
    if url and token:
        return SecopScraperHttpAdapter(base_url=url, internal_token=token)
    return NullSecopScraperAdapter()


__all__ = [
    "CaptchaRequiredError",
    "NullSecopScraperAdapter",
    "ScrapedDocDTO",
    "ScrapeResult",
    "ScraperUnavailableError",
    "SecopScraperHttpAdapter",
    "SecopScraperPort",
    "get_secop_scraper",
]
