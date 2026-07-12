"""Factory and re-exports for the SECOP II scraper adapter."""

from __future__ import annotations

from app.adapters.secop_scraper.dto import (
    CaptchaRequiredError,
    ScrapedDocDTO,
    ScrapeResult,
    ScraperUnavailableError,
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

    NOTE: this factory does NOT check ``SECOP_SCRAPER_ENABLED`` — it only looks
    at URL/token. Callers that must respect the feature flag (Slice 3's manual
    trigger) should use :func:`get_secop_scraper_gated` instead. Kept as-is for
    backward compatibility with existing callers/tests.
    """
    url = getattr(settings, "SECOP_SCRAPER_URL", "") or ""
    token = getattr(settings, "SECOP_SCRAPER_INTERNAL_TOKEN", "") or ""
    if url and token:
        return SecopScraperHttpAdapter(base_url=url, internal_token=token)
    return NullSecopScraperAdapter()


def get_secop_scraper_gated() -> SecopScraperPort:
    """Adapter selection gated by ``SECOP_SCRAPER_ENABLED`` (D-adapter-selection rule).

    The manual "Exploración Agéntica" scraper trigger (secop-document-scraper,
    Slice 3) must default to fully inert even when URL/token happen to be
    configured in an environment — the flag is the single source of truth for
    "is scraping allowed here at all". Delegates to :func:`get_secop_scraper`
    for the URL/token check once the flag is on.
    """
    if not getattr(settings, "SECOP_SCRAPER_ENABLED", False):
        return NullSecopScraperAdapter()
    return get_secop_scraper()


__all__ = [
    "CaptchaRequiredError",
    "NullSecopScraperAdapter",
    "ScrapeResult",
    "ScrapedDocDTO",
    "ScraperUnavailableError",
    "SecopScraperHttpAdapter",
    "SecopScraperPort",
    "get_secop_scraper",
    "get_secop_scraper_gated",
]
