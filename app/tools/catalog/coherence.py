"""Tool wrapper over `coherence_validator_service` — pre-radicación coherence check.

Read-only: never blocks or mutates anything by itself. `radicar_cuenta` runs the
same rule engine internally (flag-guarded by `COHERENCE_GATE_ENABLED`) to gate
submission; this tool exists so a caller (agent/MCP client) can preview findings
before attempting to radicar.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict

from pydantic import BaseModel, Field

from app.schemas.coherence import FindingOut, ValidarCoherenciaResponse
from app.services import coherence_validator_service
from app.services.coherence_validator_service import Severity
from app.tools.context import ToolContext
from app.tools.registry import tool


class ValidarCoherenciaInput(BaseModel):
    cuenta_id: uuid.UUID = Field(description="CuentaCobro id to run the coherence rule catalog over.")


@tool(
    name="validar_coherencia_cuenta",
    description=(
        "Run the pre-radicación coherence rule catalog (R1-R6) over a cuenta de cobro: stale "
        "internal cuota numbering, copied accumulated value/seg-social block, PILA planilla "
        "mismatch, stale month names in filenames, obligación text-mapping ambiguity, and stale "
        "clause text after a contract Adición. Read-only — never blocks or mutates anything "
        "itself; `radicar_cuenta` uses the same rule engine internally to gate submission. Args: "
        "cuenta_id (UUID of the cuenta de cobro; must belong to the authenticated user)."
    ),
    input_model=ValidarCoherenciaInput,
    output_model=ValidarCoherenciaResponse,
    tags=("read",),
)
async def validar_coherencia_cuenta(ctx: ToolContext, params: ValidarCoherenciaInput) -> ValidarCoherenciaResponse:
    findings = await coherence_validator_service.validar_coherencia(ctx.db, ctx.usuario_id, params.cuenta_id)
    return ValidarCoherenciaResponse(
        cuenta_cobro_id=params.cuenta_id,
        findings=[FindingOut(**asdict(f)) for f in findings],
        tiene_hard=any(f.severity == Severity.HARD for f in findings),
    )
