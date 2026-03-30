"""Agent service — orchestrates LangGraph agent execution and conversation persistence."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import build_graph
from app.agent.state import AgentState
from app.models.conversacion import Conversacion
from app.schemas.agent import AgentMode, ChatMessageResponse, LLMMessage

logger = structlog.get_logger("services.agent")

# Compile graph once at module level
_graph = build_graph()


async def chat(
    db: AsyncSession,
    user_id: uuid.UUID,
    message: str,
    session_id: uuid.UUID | None = None,
) -> ChatMessageResponse:
    """Send a message to the agent and get a response, persisting history."""

    # Load or create conversation
    if session_id:
        result = await db.execute(
            select(Conversacion).where(
                Conversacion.id == session_id,
                Conversacion.usuario_id == user_id,
            )
        )
        convo = result.scalar_one_or_none()
    else:
        convo = None

    if convo is None:
        convo = Conversacion(usuario_id=user_id, mensajes_json=[])
        db.add(convo)
        await db.flush()

    # Rebuild message history from stored JSON
    history = [LLMMessage(**m) for m in convo.mensajes_json]

    # Build initial state
    state: AgentState = {
        "session_id": convo.id,
        "user_id": user_id,
        "mode": AgentMode.CHAT,
        "messages": history,
        "user_input": message,
        "response": "",
    }

    # Run agent graph
    result_state = await _graph.ainvoke(state)

    response_text: str = result_state.get("response", "")

    # Persist messages
    convo.mensajes_json = [m.model_dump() for m in result_state.get("messages", [])]
    await db.commit()

    await logger.ainfo("agent_chat", session_id=str(convo.id), user_id=str(user_id))

    return ChatMessageResponse(
        session_id=convo.id,
        content=response_text,
        tokens_used=0,
    )


async def get_conversation_history(
    db: AsyncSession,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
) -> list[dict[str, str]]:
    """Retrieve full conversation history for a session."""
    result = await db.execute(
        select(Conversacion).where(
            Conversacion.id == session_id,
            Conversacion.usuario_id == user_id,
        )
    )
    convo = result.scalar_one_or_none()
    if convo is None:
        return []
    return convo.mensajes_json  # type: ignore[return-value]
