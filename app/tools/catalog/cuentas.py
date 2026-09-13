"""Tool wrappers over `cuenta_cobro_service` — create and submit (radicar) cuentas de cobro.

Ownership and credit checks stay in `cuenta_cobro_service`; these wrappers only
adapt `ToolContext` to the service's `(db, usuario_id, ...)` calling convention.
"""

from __future__ import annotations

import unicodedata
import uuid
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.adapters.storage import get_storage as _get_storage
from app.core.config import settings
from app.schemas.cuenta_cobro import CuentaCobroCreate, CuentaCobroResponse, GenerarPDFResponse
from app.services import cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.registry import tool

# Spanish month names -> 1-12, used to coerce a local LLM's natural "febrero" phrasing
# into the integer `CuentaCobroCreate.mes` expects. "setiembre" is an accepted spelling
# variant of "septiembre" in Spanish.
_MESES_ES: dict[str, int] = {
    "enero": 1,
    "febrero": 2,
    "marzo": 3,
    "abril": 4,
    "mayo": 5,
    "junio": 6,
    "julio": 7,
    "agosto": 8,
    "septiembre": 9,
    "setiembre": 9,
    "octubre": 10,
    "noviembre": 11,
    "diciembre": 12,
}


def _fold_accents(text: str) -> str:
    """Strip diacritics so "Diciembre"/"diciembre" and any accented input compare
    the same way (defensive — Spanish month names have none, but this keeps the
    lookup tolerant of stray accents/casing from the model)."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


class CrearCuentaCobroInput(BaseModel):
    """Tool-specific input for `crear_cuenta_cobro`.

    Mirrors `CuentaCobroCreate` (the shared REST schema) field-for-field, but
    additionally accepts a Spanish month NAME for `mes` (e.g. "febrero") — a local
    LLM (llama3.1:8b) naturally reasons about months by name, not by number, and a
    live failure showed it calling this tool with `mes` missing after the user only
    said "creá la cuenta de febrero". The lenient parsing lives ONLY here, never on
    `CuentaCobroCreate` itself, which stays strict/numeric-only for the REST API.
    """

    contrato_id: uuid.UUID
    mes: int = Field(
        ge=1,
        le=12,
        description="Mes de la cuenta de cobro: entero 1-12, o nombre del mes en español (ej. 'febrero').",
    )
    anio: int = Field(ge=2000, le=2099)
    valor: Decimal | None = Field(
        default=None,
        gt=0,
        decimal_places=2,
        description="Optional. Defaults to contrato.valor_mensual when not provided.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "contrato_id": "00000000-0000-0000-0000-000000000000",
                "mes": 3,
                "anio": 2025,
                "valor": "2000000.00",
            }
        }
    }

    @field_validator("mes", mode="before")
    @classmethod
    def _coerce_month_name(cls, value: Any) -> Any:
        """Accept a Spanish month name (or a numeric string like "2") for `mes`.

        Any other string that isn't a recognized month name is returned UNCHANGED
        so the normal `int`/`ge`/`le` validation raises a clear ValidationError —
        this never guesses.
        """
        if not isinstance(value, str):
            return value
        normalized = _fold_accents(value.strip().lower())
        if normalized.isdigit():
            return int(normalized)
        return _MESES_ES.get(normalized, value)


@tool(
    name="crear_cuenta_cobro",
    description=(
        "Create a new cuenta de cobro (monthly invoice) in BORRADOR state for one of the "
        "authenticated user's contratos, deducting credits. Fails if the contrato isn't the "
        "user's, credits are insufficient, or a cuenta already exists for that contrato/mes/anio. "
        "The checklist is NOT created yet — a separate step materializes it once the requisitos "
        "mode is chosen. REQUIRES A REAL contrato_id UUID — call `listar_contratos` FIRST if the "
        "user didn't give you one explicitly; never invent a UUID or placeholder string. "
        "REQUIRES BOTH mes AND anio — never omit anio even if only the month was mentioned. Args: "
        "contrato_id (UUID, from listar_contratos), mes (integer 1-12, or a Spanish month name "
        "such as 'febrero'), anio (integer 2000-2099), valor (optional; defaults to the "
        "contrato's valor_mensual). Example: "
        '{"contrato_id": "<uuid from listar_contratos>", "mes": 7, "anio": 2026}.'
    ),
    input_model=CrearCuentaCobroInput,
    output_model=CuentaCobroResponse,
    tags=("write",),
    consumes_credits=settings.CREDITS_PER_CUENTA_COBRO,
)
async def crear_cuenta_cobro(ctx: ToolContext, params: CrearCuentaCobroInput) -> CuentaCobroResponse:
    payload = CuentaCobroCreate(**params.model_dump())
    return await cuenta_cobro_service.crear_cuenta_cobro(ctx.db, ctx.usuario_id, payload)


class RadicarCuentaInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to submit (radicar).")


@tool(
    name="radicar_cuenta",
    description=(
        "Submit (radicar) a cuenta de cobro: BORRADOR/RECHAZADA -> ENVIADA. Runs TWO gates in "
        "order first: (1) coherence — the R1-R6 rule catalog (same one validar_coherencia_cuenta "
        "previews); any HARD finding raises code=COHERENCE_CHECK_FAILED listing the failing "
        "rule(s) (SOFT findings never block, only surface as advertencias_coherencia on success); "
        "(2) checklist — rebuilds it and, if any obligatorio requisito is still pending, raises "
        "code=CHECKLIST_INCOMPLETE naming them. How to react: on COHERENCE_CHECK_FAILED, call "
        "validar_coherencia_cuenta for the full finding list and tell the user what to fix (e.g. "
        "stale cuota numbering, mismatched SS planilla) — no tool silences a HARD finding, the "
        "underlying data must change. On CHECKLIST_INCOMPLETE, call resumen_checklist (or "
        "obtener_estado_radicacion) to see which requisitos are pending, resolve each via "
        "marcar_requisito (no_aplica/cumplido_manual) or by linking a document, then retry. Args: "
        "cuenta_id (UUID of the cuenta de cobro; must belong to the authenticated user)."
    ),
    input_model=RadicarCuentaInput,
    output_model=CuentaCobroResponse,
    tags=("write",),
)
async def radicar_cuenta(ctx: ToolContext, params: RadicarCuentaInput) -> CuentaCobroResponse:
    return await cuenta_cobro_service.radicar_cuenta(ctx.db, ctx.usuario_id, params.cuenta_id)


class GenerarCuentaCobroPdfInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to render to PDF.")


@tool(
    name="generar_cuenta_cobro_pdf",
    description=(
        "Render a cuenta de cobro to a PDF document using the user's custom template (or the "
        "built-in default), upload it to storage, and return a 1-hour presigned download URL — "
        "the agent-tool twin of `POST /cuentas-cobro/{id}/generar-pdf`. Does NOT check checklist "
        "completeness or cuenta estado; it renders whatever activities/data exist right now, same "
        "as the existing HTTP endpoint. Args: cuenta_id (UUID of the cuenta de cobro; must belong "
        "to the authenticated user)."
    ),
    input_model=GenerarCuentaCobroPdfInput,
    output_model=GenerarPDFResponse,
    tags=("write",),
)
async def generar_cuenta_cobro_pdf(ctx: ToolContext, params: GenerarCuentaCobroPdfInput) -> GenerarPDFResponse:
    storage = _get_storage(settings.S3_BUCKET_PDFS)
    return await cuenta_cobro_service.generar_pdf(ctx.db, ctx.usuario_id, params.cuenta_id, storage)
