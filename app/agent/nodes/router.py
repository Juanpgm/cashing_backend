"""Router node — classify user intent and pick execution mode."""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.system import ROUTER_PROMPT
from app.agent.state import AgentState
from app.schemas.agent import AgentMode, LLMMessage

logger = structlog.get_logger("agent.router")


async def router_node(state: AgentState) -> AgentState:
    """Determine if input is chat, pipeline or config request."""
    user_input = state.get("user_input", "")
    if not user_input:
        return {**state, "mode": AgentMode.CHAT, "error": "Empty input"}

    llm = get_llm()
    messages = [
        LLMMessage(role="system", content=ROUTER_PROMPT),
        LLMMessage(role="user", content=user_input),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=20)
        text = resp.content.strip().lower()

        if "pipeline" in text:
            mode = AgentMode.PIPELINE
        elif "config" in text:
            mode = AgentMode.CONFIG
        else:
            mode = AgentMode.CHAT

        await logger.ainfo("router_decision", mode=mode, input_preview=user_input[:80])
        return {**state, "mode": mode}
    except Exception as exc:
        await logger.awarning("router_fallback_to_chat", error=str(exc))
        return {**state, "mode": AgentMode.CHAT}
