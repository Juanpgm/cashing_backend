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

from app.core.config import settings
from app.schemas.cuenta_cobro import CuentaCobroCreate, CuentaCobroResponse
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
    anio: int = Field(ge=2020, le=2099)
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
        "authenticated user's contratos, deducting credits from their balance. Fails if the "
        "contrato doesn't belong to the user, if credits are insufficient, or if a cuenta "
        "already exists for that contrato/mes/anio. The checklist is NOT created yet — it is "
        "materialized once the requisitos mode is chosen (a separate step). REQUIRES A REAL "
        "contrato_id UUID — if the user didn't give you one explicitly, call `listar_contratos` "
        "FIRST to obtain it; never invent a UUID or pass a placeholder/description string. "
        "REQUIRES BOTH mes AND anio — never omit anio even if only the month was emphasized in "
        "the request. Args: contrato_id (UUID, from listar_contratos), mes (integer 1-12, OR a "
        "Spanish month name such as 'febrero'), anio (integer 2020-2099, e.g. 2026), valor "
        "(optional; defaults to the contrato's valor_mensual when omitted). Example call: "
        "{\"contrato_id\": \"<uuid from listar_contratos>\", \"mes\": 7, \"anio\": 2026}."
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
        "Submit (radicar) a cuenta de cobro, transitioning it from BORRADOR/RECHAZADA to "
        "ENVIADA. Gates the transition on checklist readiness: rebuilds the document checklist "
        "and raises a ValidationError (code=CHECKLIST_INCOMPLETE) naming the pending requisitos "
        "if it isn't complete yet. Args: cuenta_id (UUID of the cuenta de cobro; must belong to "
        "the authenticated user)."
    ),
    input_model=RadicarCuentaInput,
    output_model=CuentaCobroResponse,
    tags=("write",),
)
async def radicar_cuenta(ctx: ToolContext, params: RadicarCuentaInput) -> CuentaCobroResponse:
    return await cuenta_cobro_service.radicar_cuenta(ctx.db, ctx.usuario_id, params.cuenta_id)
