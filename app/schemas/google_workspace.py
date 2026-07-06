"""Schemas para Google Workspace — OAuth, Gmail, Drive."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

# ── OAuth ────────────────────────────────────────────────────────────────────


class GoogleConnectURLResponse(BaseModel):
    """URL para redirigir al usuario al flujo OAuth de Google."""

    authorization_url: str
    state: str


class GoogleOAuthCallbackRequest(BaseModel):
    code: str
    state: str


class GoogleIntegrationStatus(BaseModel):
    """Estado actual de la integración Google del usuario."""

    connected: bool
    email: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    gmail_enabled: bool = False
    drive_enabled: bool = False


# ── Email (Gmail) ────────────────────────────────────────────────────────────


class EmailAttachmentResponse(BaseModel):
    attachment_id: str
    filename: str
    mime_type: str
    size_bytes: int


class EmailMessageResponse(BaseModel):
    id: str
    thread_id: str
    subject: str
    sender: str
    recipients: list[str]
    date: datetime
    snippet: str
    body_plain: str
    body_html: str | None = None
    attachments: list[EmailAttachmentResponse] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class EmailSearchRequest(BaseModel):
    query: str = Field(description="Gmail search query (e.g. 'subject:acta after:2025/01/01')")
    max_results: int = Field(default=20, ge=1, le=100)


class EmailSearchResponse(BaseModel):
    messages: list[EmailMessageResponse]
    total: int
    query_used: str


class EmailSendRequest(BaseModel):
    to: list[EmailStr] = Field(min_length=1)
    subject: str = Field(min_length=1, max_length=500)
    body_html: str = Field(min_length=1)
    cuenta_cobro_id: uuid.UUID | None = Field(
        default=None,
        description="Si se provee, adjunta el PDF de esta cuenta de cobro al correo",
    )


class EmailSendResponse(BaseModel):
    message_id: str
    sent_to: list[str]
    subject: str


# ── Drive ────────────────────────────────────────────────────────────────────


class DriveFileResponse(BaseModel):
    id: str
    name: str
    mime_type: str
    size_bytes: int
    created_at: datetime
    modified_at: datetime
    web_view_link: str
    download_link: str | None = None


class DriveUploadRequest(BaseModel):
    cuenta_cobro_id: uuid.UUID = Field(description="PDF de esta cuenta de cobro a subir")
    make_shareable: bool = Field(
        default=False,
        description="Crear enlace público de solo lectura",
    )


class DriveUploadResponse(BaseModel):
    file_id: str
    name: str
    folder_path: list[str]
    web_view_link: str
    share_link: str | None = None


class DriveFolderResponse(BaseModel):
    folder_id: str
    path: list[str]
    files: list[DriveFileResponse] = Field(default_factory=list)


# ── Evidence collection (agent) ──────────────────────────────────────────────


class EvidenceCollectionRequest(BaseModel):
    """Request para que el agente busque evidencias de correo para una cuenta de cobro."""

    cuenta_cobro_id: uuid.UUID
    obligacion_ids: list[uuid.UUID] = Field(
        default_factory=list,
        description="Si se omite, busca para todas las obligaciones del contrato",
    )
    fecha_inicio: str = Field(description="YYYY/MM/DD — inicio del período a buscar")
    fecha_fin: str = Field(description="YYYY/MM/DD — fin del período")
    max_emails_per_query: int = Field(default=10, ge=1, le=50)


class EvidenceMatch(BaseModel):
    obligacion_id: uuid.UUID
    obligacion_descripcion: str
    email_id: str
    email_subject: str
    email_date: datetime
    email_sender: str
    relevancia: Literal["alta", "media", "baja"]
    actividad_sugerida: str
    justificacion: str


class EvidenceCollectionResponse(BaseModel):
    cuenta_cobro_id: uuid.UUID
    matches: list[EvidenceMatch]
    total_emails_analizados: int
    queries_usadas: list[str]
    avisos: list[str] = Field(default_factory=list)


# ── Evidence discovery (explorer agent: Gmail + Drive + Calendar → justificación) ──


class ObligacionInput(BaseModel):
    """Una obligación a justificar. ``id`` es opcional (se asigna por índice si falta)."""

    id: str | None = None
    descripcion: str = Field(min_length=1)


class EvidenceDiscoveryRequest(BaseModel):
    """Dispara el agente 'explorer': busca evidencias en Gmail, Drive y Calendar y genera
    la justificación por obligación para montar la Cuenta de Cobro / Radicación."""

    obligaciones: list[ObligacionInput] = Field(
        default_factory=list,
        description="Obligaciones a justificar. Requerido si no se envía contrato_id.",
    )
    contrato_id: uuid.UUID | None = Field(
        default=None,
        description="Si se provee, carga las obligaciones del contrato desde la base de datos.",
    )
    fecha_inicio: str = Field(description="YYYY-MM-DD — inicio del período a explorar")
    fecha_fin: str = Field(description="YYYY-MM-DD — fin del período")
    supervisor_email: str | None = Field(
        default=None, description="Correo del supervisor (mejora la búsqueda en Gmail)"
    )
    entidad: str | None = Field(default=None, description="Nombre de la entidad contratante")


class EvidenceLink(BaseModel):
    source: Literal["email", "drive", "calendar", "local_file"]
    titulo: str
    link: str
    fecha: str = ""


class ObligacionJustificada(BaseModel):
    obligacion_id: str
    descripcion: str
    justificacion: str
    evidencias: list[EvidenceLink] = Field(default_factory=list)


class EvidenceDiscoveryResponse(BaseModel):
    obligaciones: list[ObligacionJustificada]
    resumen: str
    total_evidencias: int
    fuentes: dict[str, int] = Field(
        default_factory=dict,
        description="Conteo de evidencias por fuente: email/drive/calendar",
    )


# ── Integration test responses ───────────────────────────────────────────────


class DriveFileTestItem(BaseModel):
    id: str
    name: str
    mime_type: str
    modified_at: datetime
    web_view_link: str


class DriveTestResponse(BaseModel):
    files: list[DriveFileTestItem]
    total: int


class CalendarEventItem(BaseModel):
    id: str
    summary: str
    start: str
    end: str
    location: str | None = None
    html_link: str | None = None


class CalendarTestResponse(BaseModel):
    events: list[CalendarEventItem]
    total: int
