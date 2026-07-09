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

# Graph is built at startup via initialise_graph(); until then it starts
# without a checkpointer so that the module can be imported during tests.
_graph = build_graph()


def initialise_graph() -> None:
    """Replace the module-level graph with a fresh compiled instance.

    Call this once inside the FastAPI lifespan after startup is complete.
    """
    global _graph
    _graph = build_graph()


def get_graph() -> object:
    """Return the compiled LangGraph graph (singleton)."""
    return _graph


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
        "_db": db,  # allows obligation/extraction nodes to persist results
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
        tokens_used=int(result_state.get("tokens_used", 0) or 0),
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


async def run_full(
    db: AsyncSession,
    session_id: uuid.UUID,
    state: AgentState,
    *,
    start_node: str | None = None,
) -> "RunResult":  # type: ignore[name-defined]  # noqa: F821
    """Execute graph. Persist checkpoint on HIL pause; update AgentRun on completion."""
    from app.agent.checkpoint import save_checkpoint
    from app.agent.engine import RunResult
    from app.models.agent_run import AgentRun

    graph = get_graph()
    result: RunResult = await graph.run(state, start_node=start_node)  # type: ignore[attr-defined]

    agent_run_id = state.get("agent_run_id")

    if result.status == "paused":
        await save_checkpoint(
            db, session_id, result.state, result.paused_node, estado="pausado_hil"
        )
        if agent_run_id:
            run_q = await db.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
            agent_run = run_q.scalar_one_or_none()
            if agent_run:
                agent_run.estado = "pausado_hil"
                agent_run.nodo_actual = result.paused_node
                await db.flush()
    else:
        if agent_run_id:
            run_q = await db.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
            agent_run = run_q.scalar_one_or_none()
            if agent_run:
                agent_run.estado = "completado"
                await db.flush()

    return result


async def resume(
    db: AsyncSession,
    session_id: uuid.UUID,
    feedback: str,
) -> "RunResult":  # type: ignore[name-defined]  # noqa: F821
    """Load checkpoint, inject hil_feedback, and resume graph execution."""
    from app.agent.checkpoint import load_checkpoint

    state, paused_node = await load_checkpoint(db, session_id)
    state = {**state, "_db": db, "hil_feedback": feedback}  # type: ignore[misc]
    return await run_full(db, session_id, state, start_node=paused_node)
