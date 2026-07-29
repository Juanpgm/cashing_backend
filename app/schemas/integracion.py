"""Schemas for the generalized `integraciones` credential store."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.models.integracion import IntegrationProvider


class IntegrationStatus(BaseModel):
    """Estado de una integración (Google o Microsoft) del usuario.

    Replaces the hardcoded `GoogleIntegrationStatus` shape with a
    provider-discriminated schema shared across providers.

    BREAKING CHANGE for existing Google consumers of GET /integraciones/google/status:
    `GoogleIntegrationStatus.gmail_enabled` is replaced here by the provider-neutral
    `mail_enabled` (plus the new `calendar_enabled`). This is deliberate and requires
    frontend coordination — see openspec/changes/microsoft-365-integration/apply-progress.md.
    """

    provider: IntegrationProvider
    connected: bool
    email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    # Per-source enabled flags, provider-neutral names.
    mail_enabled: bool = False  # gmail / outlook mail
    drive_enabled: bool = False  # drive / onedrive
    calendar_enabled: bool = False  # google calendar / outlook calendar
