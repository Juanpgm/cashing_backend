"""Content classifier tool — classify activities as laboral/non-laboral."""

from __future__ import annotations

from app.adapters.llm import get_llm
from app.agent.prompts.classification import CLASSIFICATION_PROMPT
from app.schemas.agent import LLMMessage


async def classify_content(text: str) -> str:
    """Use LLM to classify content as laboral / no_laboral / parcial."""
    llm = get_llm()
    messages = [
        LLMMessage(role="system", content=CLASSIFICATION_PROMPT),
        LLMMessage(role="user", content=text[:4000]),
    ]
    resp = await llm.complete(messages, temperature=0.0, max_tokens=1024)
    return resp.content
