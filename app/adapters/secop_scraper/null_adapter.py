"""Null adapter — used in tests and when the scraper service is not configured.

Always returns an empty result. The "agentic" mode therefore degrades to
"normal" behavior, which keeps tests fast and deterministic.
"""

from __future__ import annotations

from app.adapters.secop_scraper.dto import ScrapeResult


class NullSecopScraperAdapter:
    async def fetch_contract_docs(
        self,
        notice_uid: str,
        ref_contrato: str | None = None,
    ) -> ScrapeResult:
        return ScrapeResult(docs=[], duration_ms=0, captcha_solved=False)
