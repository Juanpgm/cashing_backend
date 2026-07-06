"""Requirements ingestion node — parses entity guide/email into EntityRequirements."""

from __future__ import annotations

import json
import re

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.requirements import REQUIREMENTS_SYSTEM, REQUIREMENTS_USER
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.requirements_ingestion")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_requirements(content: str) -> dict:
    """Extract JSON dict from LLM response."""
    match = _JSON_RE.search(content)
    if not match:
        return {}
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return {}


async def requirements_ingestion_node(state: AgentState) -> AgentState:
    """Parse entity guide/email/text into structured EntityRequirements.

    Reads: document_text OR user_input (as fallback)
    Writes: entity_requirements, current_phase
    """
    documento = state.get("document_text") or state.get("user_input") or ""
    if not documento:
        return {
            **state,
            "error": "document_text requerido para extraer requisitos de la entidad",
            "current_phase": "requirements_ingestion",
        }

    # Skip LLM in test mode
    if str(documento).startswith("__"):
        return {
            **state,
            "entity_requirements": {"entidad": "test", "campos_requeridos": []},
            "current_phase": "requirements_ingestion",
        }

    llm = get_llm(model="gemini/gemini-2.5-flash")
    messages = [
        LLMMessage(role="system", content=REQUIREMENTS_SYSTEM),
        LLMMessage(
            role="user",
            content=REQUIREMENTS_USER.replace("{documento}", documento[:8000]),
        ),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.1, max_tokens=1024)
    except Exception as exc:
        await logger.awarning("requirements_ingestion_llm_failed", error=str(exc))
        return {
            **state,
            "error": f"Error extrayendo requisitos: {exc}",
            "current_phase": "requirements_ingestion",
        }

    requirements = _parse_requirements(resp.content)
    if not requirements:
        await logger.awarning("requirements_ingestion_parse_failed", content=resp.content[:200])
        return {
            **state,
            "entity_requirements": {},
            "current_phase": "requirements_ingestion",
            "response": "No pude extraer los requisitos del documento. ¿Puedes describirlos manualmente?",
        }

    await logger.ainfo(
        "requirements_ingestion_done",
        entidad=requirements.get("entidad"),
        tokens=resp.total_tokens,
    )
    return {
        **state,
        "entity_requirements": requirements,
        "current_phase": "requirements_ingestion",
    }
