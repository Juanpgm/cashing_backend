"""PlantillaOrganismo model — per-organism ingested informe template structure.

Persists the STRUCTURE (not the raw file) extracted from an institutional
informe-de-actividades/informe-de-supervisión template so `adaptive-informe-
generation` (slice #6) and the evidence packager (`informe_service`, slice #2
stub `_resolver_estructura_organismo`) can adapt their output per organism
instead of emitting one fixed layout that matches none of them
(billing-resilience-templates, slice #5, migration 027).

`tipo_documento`/`formato` are plain strings (not `Enum` columns), per design
D5's literal field shape — this deliberately avoids adding a new Postgres enum
type (and its associated `values_callable` label-mismatch gotcha class, see
`PosicionCuota`/`TipoAdicion`) for two small, closed-ish vocabularies that are
validated at the schema layer instead.

`estructura_json` shape (superset of design D5's shorthand — see module
docstring in `requisito_inference_service` for the deviation rationale):
``{"columnas": [str], "secciones": [str], "anexo_refs": [str], "notas": str}``.
`anexo_refs` holds the literal anexo-reference strings verbatim (spec:
"Anexo reference preserved verbatim"), not a bare boolean flag.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDMixin


class PlantillaOrganismo(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "plantillas_organismo"
    __table_args__ = (
        UniqueConstraint("usuario_id", "entidad_normalizada", "tipo_documento", name="uq_plantilla_organismo_key"),
    )

    usuario_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("usuarios.id"), nullable=False, index=True)
    # Free-text organism name as it appears on `Contrato.entidad` (display value).
    entidad: Mapped[str] = mapped_column(String(255), nullable=False)
    # Normalized match key (`text_match.normalize`: lowercase, accent-stripped,
    # whitespace-collapsed) — the actual lookup key, tolerant of the free-text
    # noise `Contrato.entidad` carries across contracts of the same organism.
    entidad_normalizada: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # "informe_actividades" | "informe_supervision" — mirrors
    # `TipoDocumentoFuente` values but stored as plain str (see module docstring).
    tipo_documento: Mapped[str] = mapped_column(String(50), nullable=False)
    # "docx" | "pdf" — source template format.
    formato: Mapped[str] = mapped_column(String(20), nullable=False)
    # JSON (not JSONB) on the model for aiosqlite test-DB compatibility — the
    # migration creates the column as Postgres JSONB (same pattern as
    # `AgentCheckpoint.state_json`).
    estructura_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fuente_documento_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("documentos_fuente.id", ondelete="SET NULL"), nullable=True
    )
