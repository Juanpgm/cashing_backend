"""Playwright-based scraper for SECOP II contract documents.

Strategy:
1. Reuse a persisted `storage_state.json` (cookies + localStorage) so we do
   not need to solve a captcha for every request.
2. Open the OpportunityDetail page for the given `notice_uid`.
3. Click on the "Contratos" / "Documentos del Contrato" tab if present.
4. Wait for document list nodes; extract `<a>` href + filename.
5. Persist updated storage state on success.

If the captcha challenge is detected (page only contains a recaptcha frame),
we surface a 503 to the caller so the user can refresh manually.

Notes
-----
* Some selectors below are conservative — once Phase 1 reverse-engineering
  finishes, we can tighten them with the exact selectors observed.
* The scraper *never* downloads binaries; it only collects URLs + metadata.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import structlog
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    TimeoutError as PWTimeoutError,
    async_playwright,
)

from app.config import settings
from app.models import ScrapedDoc

log = structlog.get_logger("secop_scraper.scraper")


# Heuristic CSS / XPath selectors. Tuned during reverse-eng phase.
_DOC_LINK_SELECTORS = [
    "a[href*='RetrieveFile']",
    "a[href*='/Public/Archive/']",
]
_TAB_CONTRACT_DOCS_TEXTS = [
    "Documentos del Contrato",
    "Contratos",
    "Documentos",
]
_CAPTCHA_MARKERS = [
    "iframe[src*='recaptcha']",
    "div.g-recaptcha",
]


class CaptchaRequired(RuntimeError):
    """Raised when the page is gated by reCAPTCHA."""


class SecopScraper:
    """Singleton-ish scraper. Owns one browser; spawns a context per scrape."""

    def __init__(self) -> None:
        self._pw = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._browser:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=settings.SECOP_HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        log.info("scraper.started", headless=settings.SECOP_HEADLESS)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._pw:
            await self._pw.stop()
            self._pw = None

    async def _new_context(self) -> BrowserContext:
        if not self._browser:
            await self.start()
        assert self._browser is not None
        kwargs: dict[str, Any] = {
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "locale": "es-CO",
            "viewport": {"width": 1280, "height": 900},
        }
        storage = Path(settings.SECOP_STORAGE_STATE_PATH)
        if storage.exists():
            kwargs["storage_state"] = str(storage)
        ctx = await self._browser.new_context(**kwargs)
        # Light stealth: remove navigator.webdriver
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        return ctx

    async def _persist_storage(self, ctx: BrowserContext) -> None:
        try:
            storage = Path(settings.SECOP_STORAGE_STATE_PATH)
            storage.parent.mkdir(parents=True, exist_ok=True)
            await ctx.storage_state(path=str(storage))
        except Exception as exc:
            await log.awarning("scraper.persist_storage_failed", error=str(exc))

    async def _is_captcha_blocked(self, page: Page) -> bool:
        for sel in _CAPTCHA_MARKERS:
            try:
                el = await page.query_selector(sel)
                if el:
                    title = await page.title()
                    # The plain ReCaptcha title page means we're fully blocked
                    if "ReCaptcha" in title or "Captcha" in title:
                        return True
            except Exception:
                pass
        return False

    async def _click_contract_tab(self, page: Page) -> None:
        for label in _TAB_CONTRACT_DOCS_TEXTS:
            try:
                # Try by accessible name (button/link/tab)
                loc = page.get_by_role("tab", name=re.compile(label, re.I))
                if await loc.count():
                    await loc.first.click(timeout=3000)
                    return
                loc = page.get_by_text(re.compile(label, re.I))
                if await loc.count():
                    await loc.first.click(timeout=3000)
                    return
            except Exception:
                continue

    async def _extract_docs(self, page: Page) -> list[ScrapedDoc]:
        # Wait briefly for at least one document link to appear
        for sel in _DOC_LINK_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=5_000)
                break
            except PWTimeoutError:
                continue

        docs: list[ScrapedDoc] = []
        seen: set[str] = set()
        for sel in _DOC_LINK_SELECTORS:
            anchors = await page.query_selector_all(sel)
            for a in anchors:
                href = await a.get_attribute("href")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://community.secop.gov.co" + href
                if href in seen:
                    continue
                seen.add(href)

                text = (await a.inner_text() or "").strip()
                # filename: prefer link text, fallback to title attr
                title = (await a.get_attribute("title")) or ""
                nombre = text or title or "documento"
                # extract DocumentId from query string if present
                m = re.search(r"DocumentId=(\d+)", href)
                doc_id = m.group(1) if m else None
                # extract extension from filename
                ext_match = re.search(r"\.([a-zA-Z0-9]{2,5})$", nombre)
                extension = ext_match.group(1).lower() if ext_match else None

                # Try to find a sibling date (best-effort)
                fecha = None
                try:
                    parent = await a.evaluate_handle("el => el.closest('tr, li, div')")
                    if parent:
                        ptext = await parent.evaluate("el => el.innerText")
                        date_m = re.search(r"(\d{1,2}[/-]\d{1,2}[/-]\d{4})", ptext or "")
                        if date_m:
                            for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
                                try:
                                    fecha = datetime.strptime(date_m.group(1), fmt).date()
                                    break
                                except ValueError:
                                    pass
                except Exception:
                    pass

                docs.append(
                    ScrapedDoc(
                        document_id=doc_id,
                        nombre_archivo=nombre,
                        url_descarga=href,
                        fecha_carga=fecha,
                        extension=extension,
                        descripcion=None,
                    )
                )

        return docs

    async def fetch_contract_docs(
        self, notice_uid: str, ref_contrato: str | None = None
    ) -> tuple[list[ScrapedDoc], bool]:
        """Scrape the SECOP II opportunity page for a contract.

        Returns
        -------
        (docs, captcha_solved)
            ``docs`` is the list of scraped documents.
            ``captcha_solved`` is True if we had to retry past a captcha challenge.
        """
        target = (
            "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
            f"Index?noticeUID={notice_uid}&isFromPublicArea=True"
        )
        captcha_solved = False
        last_exc: Exception | None = None

        async with self._lock:
            await self.start()
            for attempt in range(1, settings.SECOP_CAPTCHA_MAX_RETRIES + 1):
                ctx = await self._new_context()
                page = await ctx.new_page()
                try:
                    await page.goto(target, wait_until="domcontentloaded", timeout=settings.SECOP_NAV_TIMEOUT_MS)
                    if await self._is_captcha_blocked(page):
                        await log.awarning(
                            "scraper.captcha_blocked", attempt=attempt, notice_uid=notice_uid
                        )
                        last_exc = CaptchaRequired("Page is captcha-gated")
                        await ctx.close()
                        continue

                    await self._click_contract_tab(page)
                    await page.wait_for_load_state("networkidle", timeout=settings.SECOP_NAV_TIMEOUT_MS)
                    docs = await self._extract_docs(page)

                    await self._persist_storage(ctx)
                    if attempt > 1:
                        captcha_solved = True
                    return docs, captcha_solved
                except CaptchaRequired as exc:
                    last_exc = exc
                except Exception as exc:
                    await log.awarning(
                        "scraper.attempt_failed", attempt=attempt, error=str(exc)
                    )
                    last_exc = exc
                finally:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

            if isinstance(last_exc, CaptchaRequired):
                raise last_exc
            raise RuntimeError(f"All scrape attempts failed: {last_exc}")


# Module-level singleton — one browser per process
scraper = SecopScraper()
