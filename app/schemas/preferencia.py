"""Preferencia schemas — request/response models for user preferences (Phase 7)."""

from typing import Literal

from pydantic import BaseModel

# The set of accepted values mirrors the <select> options on the frontend
# settings page (cashing-frontend/app/configuracion/page.tsx). Keeping this
# in sync with the UI avoids storing values the frontend can never produce.
Idioma = Literal["es", "en"]
Timezone = Literal["America/Bogota", "America/New_York", "Europe/Madrid"]


class PreferenciasResponse(BaseModel):
    """Full set of user preferences, always populated (defaults fill any gap)."""

    notificaciones_email: bool
    idioma: str
    timezone: str


class PreferenciasUpdateRequest(BaseModel):
    """Partial update — only fields explicitly set by the client are written."""

    notificaciones_email: bool | None = None
    idioma: Idioma | None = None
    timezone: Timezone | None = None
