"""Pydantic models exchanged between the main API and the scraper service."""

from __future__ import annotations

from datetime import date
from pydantic import BaseModel, Field


class ScrapeRequest(BaseModel):
    """Request body for POST /scrape/contract-docs."""

    notice_uid: str = Field(..., description="SECOP II notice UID (e.g., CO1.NTC.9506401)")
    ref_contrato: str | None = Field(None, description="Internal contract reference, only for logging")
    force_refresh: bool = Field(False, description="If true, bypass session cache and re-bootstrap")


class ScrapedDoc(BaseModel):
    """Single contract-phase document discovered in SECOP II."""

    document_id: str | None = None
    nombre_archivo: str
    url_descarga: str
    fecha_carga: date | None = None
    extension: str | None = None
    descripcion: str | None = None
    tipo_origen: str = "contrato_firmado"  # SECOP II contract-phase only


class ScrapeResponse(BaseModel):
    docs: list[ScrapedDoc]
    duration_ms: int
    captcha_solved: bool = False
    notice_uid: str
