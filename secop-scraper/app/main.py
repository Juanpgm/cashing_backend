"""FastAPI entry point for the SECOP II scraper microservice."""

from __future__ import annotations

import time
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Header, HTTPException, status

from app.config import settings
from app.models import ScrapeRequest, ScrapeResponse
from app.scraper import CaptchaRequired, scraper

log = structlog.get_logger("secop_scraper.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await scraper.start()
    log.info("service.startup")
    yield
    await scraper.stop()
    log.info("service.shutdown")


app = FastAPI(
    title="SECOP II Scraper",
    version="0.1.0",
    lifespan=lifespan,
)


def _check_internal_token(x_internal_token: str | None) -> None:
    if not x_internal_token or x_internal_token != settings.SECOP_SCRAPER_INTERNAL_TOKEN:
        raise HTTPException(status_code=401, detail="invalid_internal_token")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/scrape/contract-docs", response_model=ScrapeResponse)
async def scrape_contract_docs(
    body: ScrapeRequest,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
) -> ScrapeResponse:
    _check_internal_token(x_internal_token)

    started = time.monotonic()
    try:
        docs, captcha_solved = await scraper.fetch_contract_docs(
            notice_uid=body.notice_uid,
            ref_contrato=body.ref_contrato,
        )
    except CaptchaRequired:
        manual_url = (
            "https://community.secop.gov.co/Public/Tendering/OpportunityDetail/"
            f"Index?noticeUID={body.notice_uid}&isFromPublicArea=True"
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "captcha_required",
                "manual_action_url": manual_url,
                "message": (
                    "SECOP II requiere verificación humana. Abre el enlace, "
                    "resuelve el captcha y vuelve a intentar."
                ),
            },
        )
    except Exception as exc:
        await log.aerror("scrape.failed", notice_uid=body.notice_uid, error=str(exc))
        raise HTTPException(status_code=502, detail={"error": "scrape_failed", "message": str(exc)})

    duration_ms = int((time.monotonic() - started) * 1000)
    await log.ainfo(
        "scrape.success",
        notice_uid=body.notice_uid,
        count=len(docs),
        duration_ms=duration_ms,
        captcha_solved=captcha_solved,
    )
    return ScrapeResponse(
        docs=docs,
        duration_ms=duration_ms,
        captcha_solved=captcha_solved,
        notice_uid=body.notice_uid,
    )
