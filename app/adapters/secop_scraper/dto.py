"""DTOs exchanged between the scraper microservice and the main API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class ScrapedDocDTO:
    """Single contract-phase document discovered by the SECOP II scraper."""

    document_id: str | None
    nombre_archivo: str
    url_descarga: str
    fecha_carga: date | None
    extension: str | None
    descripcion: str | None
    tipo_origen: str = "contrato_firmado"


@dataclass(frozen=True)
class ScrapeResult:
    docs: list[ScrapedDocDTO]
    duration_ms: int
    captcha_solved: bool


class CaptchaRequiredError(RuntimeError):
    """Raised when the SECOP II scraper hit a captcha and could not continue.

    The caller is expected to surface a 503 to the end-user with a manual link.
    """

    def __init__(self, manual_action_url: str, message: str | None = None) -> None:
        super().__init__(message or "SECOP II requires manual captcha resolution")
        self.manual_action_url = manual_action_url


class ScraperUnavailableError(RuntimeError):
    """Raised when the scraper microservice is misconfigured or unreachable."""
