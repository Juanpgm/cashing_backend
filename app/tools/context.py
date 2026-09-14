"""Execution context passed to every tool handler."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usuario import Usuario


@dataclass
class ToolAttachment:
    """A file the user attached in the current chat turn, available to tool handlers.

    Held in-memory only for the lifetime of one `chat_with_tools` request — never
    persisted as-is. Tools that want to keep the file (e.g. `importar_documento`)
    must explicitly upload it via the normal document/storage services.
    """

    filename: str
    content_type: str
    data: bytes


@dataclass
class ToolContext:
    """Everything a tool handler needs to run on behalf of an authenticated user.

    Built once per invocation from an already-authenticated `Usuario` (e.g. via
    `app.core.auth.authenticate_bearer`) and passed to `app.tools.invoke.invoke_tool`.
    Handlers never authenticate or authorize on their own — they receive a context
    that is already scoped to one user.
    """

    db: AsyncSession
    usuario: Usuario
    # Files attached to the current chat turn, keyed by filename. Empty for every
    # caller except `agent_chat_service.chat_with_tools` — the MCP server and other
    # call sites never populate this, so existing tools are unaffected.
    attachments: dict[str, ToolAttachment] = field(default_factory=dict)
    # Request-scoped FastAPI `BackgroundTasks`, when the caller has one to offer
    # (radicacion-sin-friccion Phase 2 slice 2.6). Only the synchronous
    # `POST /agent/chat` route (`agent_chat_service.chat_with_tools`) threads a
    # real instance through today — every other ToolContext construction site
    # (the SSE streaming chat path, the MCP server, `descubrir_evidencias`'s and
    # `radicar_cuenta`'s direct REST endpoints, every test) leaves this `None`,
    # which is the correct default wherever no request-scoped `BackgroundTasks`
    # actually exists. Tools that want to background a follow-up unit of work
    # (e.g. `subir_evidencias_desde_chat` backgrounding evidence classification)
    # read this instead of hardcoding `None`.
    background_tasks: BackgroundTasks | None = None

    @property
    def usuario_id(self) -> uuid.UUID:
        return self.usuario.id
