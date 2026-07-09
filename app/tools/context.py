"""Execution context passed to every tool handler."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.usuario import Usuario


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

    @property
    def usuario_id(self) -> uuid.UUID:
        return self.usuario.id
