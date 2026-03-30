"""Chat API — conversational interface with SSE streaming support."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm import get_llm
from app.agent.prompts.system import SYSTEM_PROMPT
from app.api.deps import CurrentUser
from app.core.database import get_db
from app.schemas.agent import (
    ChatMessageRequest,
    ChatMessageResponse,
    ConversationHistoryResponse,
    LLMMessage,
)
from app.services import agent_service

logger = structlog.get_logger("api.chat")

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("/", response_model=ChatMessageResponse, status_code=200)
async def send_message(
    body: ChatMessageRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChatMessageResponse:
    """Send a message to the AI agent and get a response."""
    return await agent_service.chat(
        db=db,
        user_id=user.id,
        message=body.message,
        session_id=body.session_id,
    )


@router.post("/stream")
async def stream_message(
    body: ChatMessageRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Send a message and receive an SSE stream of tokens."""

    async def event_stream() -> AsyncIterator[str]:
        llm = get_llm()
        messages = [
            LLMMessage(role="system", content=SYSTEM_PROMPT),
            LLMMessage(role="user", content=body.message),
        ]
        full_response = ""
        try:
            async for token in llm.stream(messages):
                full_response += token
                yield f"data: {json.dumps({'token': token})}\n\n"
            yield f"data: {json.dumps({'done': True, 'full_response': full_response})}\n\n"
        except Exception as exc:
            await logger.aerror("stream_error", error=str(exc))
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{session_id}", response_model=ConversationHistoryResponse)
async def get_history(
    session_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConversationHistoryResponse:
    """Retrieve conversation history for a session."""
    messages = await agent_service.get_conversation_history(db, user.id, session_id)
    return ConversationHistoryResponse(session_id=session_id, messages=messages)
