"""Pydantic schemas for the pre-radicación coherence validator."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel

from app.services.coherence_validator_service import Severity


class FindingOut(BaseModel):
    rule_id: str
    severity: Severity
    codigo: str
    mensaje: str
    contexto: dict[str, Any]


class ValidarCoherenciaResponse(BaseModel):
    cuenta_cobro_id: uuid.UUID
    findings: list[FindingOut]
    tiene_hard: bool
