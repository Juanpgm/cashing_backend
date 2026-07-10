"""Free-form agent chat API — "Claude-style" tool-calling chat endpoint.

Separate router (not `agent_sessions.py`, which owns the fixed-pipeline SSE/HIL
endpoints) so this stays a small, focused surface for the free-form loop in
`app.services.agent_chat_service`.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.core.file_validation import validate_evidence_file
from app.core.rate_limit import limiter
from app.schemas.agent import AgentChatResult
from app.services import agent_chat_service
from app.tools.context import ToolAttachment

logger = structlog.get_logger("api.agent_chat")

router = APIRouter(prefix="/agent", tags=["agent"])

MAX_CHAT_FILES = 5

# Aggregate cap across all attachments in one message — a per-file cap alone still
# lets a client attach many files that are each individually small but sum to a
# large in-memory payload for this single request.
MAX_TOTAL_ATTACHMENT_BYTES = 40 * 1024 * 1024


@router.post("/chat", response_model=AgentChatResult, status_code=200)
@limiter.limit("5/minute")
async def chat(
    request: Request,
    user: CurrentUser,
    message: str = Form(..., min_length=1, max_length=5000),
    session_id: str | None = Form(None),
    contrato_id: str | None = Form(None),
    files: list[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
) -> AgentChatResult:
    """Send a free-form message (optionally with file attachments) to the tool-calling agent.

    Unlike `POST /chat/` (fixed router + pipeline graph), this endpoint lets the LLM
    decide autonomously which registered tools to call — importing contracts,
    creating cuentas de cobro, managing the checklist, generating informes, finding
    evidence, etc. — in whatever order is needed to resolve the request.

    Accepts up to 5 file attachments per message, any format except executables
    (same allowlist as evidence uploads — see `validate_evidence_file`), with a
    combined size cap of 40 MB across all attachments in the message.

    `contrato_id` is an OPTIONAL contract context (e.g. the contract the user has
    currently open in the UI) so the agent never has to ask the user for a raw UUID.
    It is resolved defensively by the service — a missing, malformed, unknown, or
    not-owned value is silently ignored (never a 4xx/5xx for this alone).
    """
    if len(files) > MAX_CHAT_FILES:
        raise ValidationError(f"Máximo {MAX_CHAT_FILES} archivos por mensaje.")

    attachments: dict[str, ToolAttachment] = {}
    total_bytes = 0
    for upload in files:
        if not upload.filename:
            raise ValidationError("Todos los archivos deben tener un nombre.")

        content = await upload.read()
        total_bytes += len(content)
        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValidationError(
                f"El total de los archivos adjuntos supera el máximo permitido de "
                f"{MAX_TOTAL_ATTACHMENT_BYTES // (1024 * 1024)} MB por mensaje."
            )
        validate_evidence_file(
            filename=upload.filename,
            size=len(content),
            content_type=upload.content_type or "application/octet-stream",
            content=content,
        )
        attachments[upload.filename] = ToolAttachment(
            filename=upload.filename,
            content_type=upload.content_type or "application/octet-stream",
            data=content,
        )

    return await agent_chat_service.chat_with_tools(
        db=db,
        usuario=user,
        message=message,
        session_id=session_id,
        attachments=attachments,
        contrato_id=contrato_id,
    )
