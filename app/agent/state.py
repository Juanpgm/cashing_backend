"""Agent state — typed dictionary for LangGraph graph."""

from __future__ import annotations

import uuid
from typing import TypedDict

from app.schemas.agent import AgentMode, LLMMessage


class AgentState(TypedDict, total=False):
    """State that flows through the LangGraph agent graph."""

    # Session
    session_id: uuid.UUID
    user_id: uuid.UUID
    mode: AgentMode

    # Conversation
    messages: list[LLMMessage]
    user_input: str
    response: str

    # Document processing
    document_id: uuid.UUID | None
    document_text: str | None
    document_metadata: dict[str, str | int | float | None] | None

    # Pipeline outputs
    extracted_data: dict[str, str | int | float | None] | None
    classification: str | None
    justification: str | None

    # Error tracking
    error: str | None
