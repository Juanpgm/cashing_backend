"""StepperStateResponse — the cross-repo aggregate readiness contract consumed by
`/radicar` (radicacion-stepper, work unit B4).

This is THE payload both repos (backend + frontend) target — kept minimal and
explicit per design section 1 (the SECOP-shape-drift precedent is the cautionary
tale: assert this shape explicitly in tests, never let it drift silently).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel

StepKey = Literal[
    "contrato",
    "cuota",
    "checklist",
    "justificaciones",
    "formato",
    "evidencias",
    "paquete",
]


class StepState(BaseModel):
    """Readiness of one of the 7 stepper steps, sourced from persisted DB state only."""

    step: int
    key: StepKey
    complete: bool
    blocking: bool
    # Reuses an EXISTING domain error code (CHECKLIST_INCOMPLETE, PACKAGE_PENDIENTE,
    # COHERENCE_CHECK_FAILED, SECRET_DETECTED_IN_PACKAGE, CUOTA_POSITION_CONFLICT)
    # or None. No new codes are invented here.
    code: str | None = None
    # Small, optional, step-specific counts — never full checklist/evidence trees.
    detail: dict[str, Any] | None = None


class StepperStateResponse(BaseModel):
    """Aggregate per-cuota step readiness. Read-only, no credits, no side effects."""

    cuenta_cobro_id: uuid.UUID
    contrato_id: uuid.UUID
    mes: int
    anio: int
    fecha_transaccion: date | None
    # Present => cuota exists => credits already charged (idempotency signal for
    # the frontend's "never re-charge on resume" rule).
    numero_cuota: int | None
    posicion_cuota: str
    current_step: int
    furthest_completed_step: int
    steps: list[StepState]
