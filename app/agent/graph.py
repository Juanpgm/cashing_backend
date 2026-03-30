"""LangGraph agent graph — routes through chat, pipeline, or config mode."""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from app.agent.nodes.chat import chat_node
from app.agent.nodes.pipeline import (
    classification_node,
    doc_ingestion_node,
    doc_understanding_node,
    justification_node,
)
from app.agent.nodes.router import router_node
from app.agent.state import AgentState
from app.schemas.agent import AgentMode


def _route_by_mode(state: AgentState) -> str:
    """Conditional edge: branch on the mode chosen by router."""
    mode = state.get("mode", AgentMode.CHAT)
    if mode == AgentMode.PIPELINE:
        return "doc_ingestion"
    if mode == AgentMode.CONFIG:
        return "chat"  # config falls back to chat for MVP
    return "chat"


def build_graph() -> StateGraph:
    """Construct and compile the CashIn agent graph."""
    graph = StateGraph(AgentState)

    # Nodes
    graph.add_node("router", router_node)
    graph.add_node("chat", chat_node)
    graph.add_node("doc_ingestion", doc_ingestion_node)
    graph.add_node("doc_understanding", doc_understanding_node)
    graph.add_node("classification", classification_node)
    graph.add_node("justification", justification_node)

    # Entry
    graph.set_entry_point("router")

    # Conditional routing from router
    graph.add_conditional_edges(
        "router",
        _route_by_mode,
        {"chat": "chat", "doc_ingestion": "doc_ingestion"},
    )

    # Chat → END
    graph.add_edge("chat", END)

    # Pipeline chain: ingestion → understanding → classification → justification → END
    graph.add_edge("doc_ingestion", "doc_understanding")
    graph.add_edge("doc_understanding", "classification")
    graph.add_edge("classification", "justification")
    graph.add_edge("justification", END)

    return graph.compile()
