"""Chat node — conversational replies powered by LLM."""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.system import SYSTEM_PROMPT
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.chat")


async def chat_node(state: AgentState) -> AgentState:
    """Generate a conversational response using full message history."""
    llm = get_llm()

    history: list[LLMMessage] = list(state.get("messages") or [])
    # Prepend system prompt
    if not history or history[0].role != "system":
        history.insert(0, LLMMessage(role="system", content=SYSTEM_PROMPT))

    # Append current user message if not already in history
    user_input = state.get("user_input", "")
    if user_input and (not history or history[-1].content != user_input):
        history.append(LLMMessage(role="user", content=user_input))

    try:
        resp = await llm.complete(history, temperature=0.4, max_tokens=2048)
        await logger.ainfo("chat_response", tokens=resp.total_tokens)
        return {
            **state,
            "response": resp.content,
            "messages": [*history, LLMMessage(role="assistant", content=resp.content)],
        }
    except Exception as exc:
        await logger.aerror("chat_error", error=str(exc))
        return {**state, "response": "Lo siento, ocurrió un error procesando tu mensaje.", "error": str(exc)}
