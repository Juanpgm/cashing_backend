"""Pipeline node — document processing and billing automation steps."""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.classification import CLASSIFICATION_PROMPT
from app.agent.prompts.extraction import EXTRACTION_PROMPT
from app.agent.prompts.justification import JUSTIFICATION_PROMPT
from app.agent.state import AgentState
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.pipeline")


async def doc_ingestion_node(state: AgentState) -> AgentState:
    """Parse and store document text — actual parsing delegated to agent tools."""
    doc_text = state.get("document_text")
    if not doc_text:
        return {**state, "error": "No document text provided for ingestion"}
    await logger.ainfo("doc_ingestion", chars=len(doc_text))
    return {**state, "error": None}


async def doc_understanding_node(state: AgentState) -> AgentState:
    """Extract structured data from document text using LLM."""
    doc_text = state.get("document_text")
    if not doc_text:
        return {**state, "error": "No document text for understanding"}

    llm = get_llm()
    messages = [
        LLMMessage(role="system", content=EXTRACTION_PROMPT),
        LLMMessage(role="user", content=doc_text[:8000]),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.1, max_tokens=4096)
        await logger.ainfo("doc_understanding", tokens=resp.total_tokens)
        return {**state, "extracted_data": {"raw_extraction": resp.content}, "error": None}
    except Exception as exc:
        await logger.aerror("doc_understanding_error", error=str(exc))
        return {**state, "error": str(exc)}


async def classification_node(state: AgentState) -> AgentState:
    """Classify extracted content (laboral vs non-laboral)."""
    extracted = state.get("extracted_data")
    if not extracted:
        return {**state, "error": "No extracted data for classification"}

    llm = get_llm()
    content = str(extracted.get("raw_extraction", ""))
    messages = [
        LLMMessage(role="system", content=CLASSIFICATION_PROMPT),
        LLMMessage(role="user", content=content[:4000]),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.0, max_tokens=512)
        await logger.ainfo("classification", result=resp.content[:80])
        return {**state, "classification": resp.content, "error": None}
    except Exception as exc:
        await logger.aerror("classification_error", error=str(exc))
        return {**state, "error": str(exc)}


async def justification_node(state: AgentState) -> AgentState:
    """Generate billing justification text from classified content."""
    classification_text = state.get("classification")
    extracted = state.get("extracted_data")
    if not classification_text or not extracted:
        return {**state, "error": "Missing classification or extracted data"}

    llm = get_llm()
    context = f"Clasificación:\n{classification_text}\n\nDatos extraídos:\n{extracted.get('raw_extraction', '')}"
    messages = [
        LLMMessage(role="system", content=JUSTIFICATION_PROMPT),
        LLMMessage(role="user", content=context[:6000]),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.3, max_tokens=4096)
        await logger.ainfo("justification", tokens=resp.total_tokens)
        return {
            **state,
            "justification": resp.content,
            "response": f"Justificación generada exitosamente.\n\n{resp.content}",
            "error": None,
        }
    except Exception as exc:
        await logger.aerror("justification_error", error=str(exc))
        return {**state, "error": str(exc)}
