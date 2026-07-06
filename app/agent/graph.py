"""Agent graph — routes through all phases of the CashIn pipeline."""

from __future__ import annotations

from app.agent.engine import END, CompiledGraph

from app.agent.nodes.activities import generate_activities_node
from app.agent.nodes.chat import chat_node
from app.agent.nodes.doc_assembly import doc_assembly_node
from app.agent.nodes.drive_upload import drive_upload_node
from app.agent.nodes.email_fetch import email_fetch_node
from app.agent.nodes.entity_profile import entity_profile_node
from app.agent.nodes.evidence_dedup import evidence_dedup_node
from app.agent.nodes.evidence_matcher import evidence_matcher_node
from app.agent.nodes.evidence_orchestrator import evidence_orchestrator_node
from app.agent.nodes.extraction import contract_metadata_node, obligations_extraction_node
from app.agent.nodes.folder_organizer import folder_organizer_node
from app.agent.nodes.human_review import human_review_node
from app.agent.nodes.local_files import local_files_node
from app.agent.nodes.pipeline import (
    classification_node,
    doc_ingestion_node,
    doc_understanding_node,
    justification_node,
)
from app.agent.nodes.quality_gate import quality_gate_node
from app.agent.nodes.requirements_ingestion import requirements_ingestion_node
from app.agent.nodes.router import router_node
from app.agent.nodes.secop_discovery import secop_discovery_node
from app.agent.nodes.supervisor import supervisor_node
from app.agent.nodes.template_resolver import template_resolver_node
from app.agent.state import AgentState
from app.schemas.agent import AgentMode


def _route_by_mode(state: AgentState) -> str:
    """Conditional edge: branch on the mode chosen by router."""
    mode = state.get("mode", AgentMode.CHAT)
    if mode == AgentMode.PIPELINE:
        return "doc_ingestion"
    if mode == AgentMode.EVIDENCE:
        return "email_fetch"
    if mode == AgentMode.DRIVE:
        return "drive_upload"
    if mode == AgentMode.EXTRACT_OBLIGATIONS:
        return "extraction_router"
    if mode == AgentMode.GENERATE_ACTIVITIES:
        return "generate_activities"
    if mode == AgentMode.SECOP_DISCOVERY:
        return "secop_discovery"
    if mode == AgentMode.REQUIREMENTS_INGESTION:
        return "requirements_ingestion"
    if mode == AgentMode.TEMPLATE_RESOLVE:
        return "template_resolver"
    if mode == AgentMode.QUALITY_GATE:
        return "quality_gate"
    if mode == AgentMode.CUENTA_COBRO_FULL:
        return "supervisor"
    # CHAT and CONFIG both go to chat (conversational config for MVP)
    return "chat"


def _route_extraction(state: AgentState) -> str:
    """When contrato_id_str is absent, extract metadata first; otherwise go straight to obligations."""
    if not state.get("contrato_id_str"):
        return "contract_metadata"
    return "obligations_extraction"


def _route_from_supervisor(state: AgentState) -> str:
    """Route to the next node based on supervisor_plan."""
    plan = state.get("supervisor_plan") or []
    if not plan:
        return "human_review"
    next_node = plan[0]
    valid = {
        "obligations_extraction",
        "quality_gate",
        "evidence_orchestrator",
        "evidence_dedup",
        "doc_assembly",
        "folder_organizer",
        "human_review",
        "END",
    }
    return next_node if next_node in valid else "human_review"


def build_graph() -> CompiledGraph:
    """Construct and compile the CashIn agent graph."""
    graph = CompiledGraph()

    # ── Core nodes ────────────────────────────────────────────────────────────
    graph.add_node("router", router_node)
    graph.add_node("chat", chat_node)
    graph.add_node("doc_ingestion", doc_ingestion_node)
    graph.add_node("doc_understanding", doc_understanding_node)
    graph.add_node("classification", classification_node)
    graph.add_node("justification", justification_node)
    graph.add_node("email_fetch", email_fetch_node)
    graph.add_node("drive_upload", drive_upload_node)
    graph.add_node("extraction_router", lambda s: s)  # pass-through dispatcher
    graph.add_node("contract_metadata", contract_metadata_node)
    graph.add_node("obligations_extraction", obligations_extraction_node)
    graph.add_node("generate_activities", generate_activities_node)
    graph.add_node("secop_discovery", secop_discovery_node)

    # ── Phase 2 nodes ─────────────────────────────────────────────────────────
    graph.add_node("requirements_ingestion", requirements_ingestion_node)
    graph.add_node("entity_profile", entity_profile_node)
    graph.add_node("template_resolver", template_resolver_node)

    # ── Phase 3 nodes ─────────────────────────────────────────────────────────
    graph.add_node("quality_gate", quality_gate_node)

    # ── Phase 4 nodes ─────────────────────────────────────────────────────────
    graph.add_node("local_files", local_files_node)
    graph.add_node("evidence_orchestrator", evidence_orchestrator_node)
    graph.add_node("evidence_matcher", evidence_matcher_node)
    graph.add_node("evidence_dedup", evidence_dedup_node)

    # ── Phase 5 nodes ─────────────────────────────────────────────────────────
    graph.add_node("doc_assembly", doc_assembly_node)
    graph.add_node("folder_organizer", folder_organizer_node)

    # ── Phase 6 nodes ─────────────────────────────────────────────────────────
    graph.add_node("supervisor", supervisor_node)
    graph.add_node("human_review", human_review_node)

    # Entry
    graph.set_entry_point("router")

    # ── Conditional routing from router ───────────────────────────────────────
    graph.add_conditional_edges(
        "router",
        _route_by_mode,
        {
            "chat": "chat",
            "doc_ingestion": "doc_ingestion",
            "email_fetch": "email_fetch",
            "drive_upload": "drive_upload",
            "extraction_router": "extraction_router",
            "generate_activities": "generate_activities",
            "secop_discovery": "secop_discovery",
            "requirements_ingestion": "requirements_ingestion",
            "template_resolver": "template_resolver",
            "quality_gate": "quality_gate",
            "supervisor": "supervisor",
        },
    )

    # Chat → END
    graph.add_edge("chat", END)

    # Pipeline chain: ingestion → understanding → classification → justification → END
    graph.add_edge("doc_ingestion", "doc_understanding")
    graph.add_edge("doc_understanding", "classification")
    graph.add_edge("classification", "justification")
    graph.add_edge("justification", END)

    # Evidence → END
    graph.add_edge("email_fetch", END)

    # Drive upload → END
    graph.add_edge("drive_upload", END)

    # Extraction chain
    graph.add_conditional_edges(
        "extraction_router",
        _route_extraction,
        {"contract_metadata": "contract_metadata", "obligations_extraction": "obligations_extraction"},
    )
    graph.add_edge("contract_metadata", "obligations_extraction")
    graph.add_edge("obligations_extraction", END)

    # Activities → END
    graph.add_edge("generate_activities", END)

    # SECOP → END
    graph.add_edge("secop_discovery", END)

    # ── Phase 2 edges ─────────────────────────────────────────────────────────
    # REQUIREMENTS_INGESTION mode: ingestion → entity_profile → template_resolver → END
    graph.add_edge("requirements_ingestion", "entity_profile")
    graph.add_edge("entity_profile", "template_resolver")
    graph.add_edge("template_resolver", END)

    # ── Phase 3 edges ─────────────────────────────────────────────────────────
    # QUALITY_GATE mode: standalone quality check → END
    graph.add_edge("quality_gate", END)

    # ── Phase 4 edges ─────────────────────────────────────────────────────────
    # local_files → END (standalone)
    graph.add_edge("local_files", END)
    # evidence_orchestrator → evidence_matcher → evidence_dedup → END
    graph.add_edge("evidence_orchestrator", "evidence_matcher")
    graph.add_edge("evidence_matcher", "evidence_dedup")
    graph.add_edge("evidence_dedup", END)

    # ── Phase 5 edges ─────────────────────────────────────────────────────────
    graph.add_edge("doc_assembly", "folder_organizer")
    graph.add_edge("folder_organizer", END)

    # ── Phase 6 edges ─────────────────────────────────────────────────────────
    # CUENTA_COBRO_FULL: supervisor decides next node dynamically
    graph.add_conditional_edges(
        "supervisor",
        _route_from_supervisor,
        {
            "obligations_extraction": "obligations_extraction",
            "quality_gate": "quality_gate",
            "evidence_orchestrator": "evidence_orchestrator",
            "evidence_dedup": "evidence_dedup",
            "doc_assembly": "doc_assembly",
            "folder_organizer": "folder_organizer",
            "human_review": "human_review",
            "END": END,
        },
    )
    graph.add_edge("human_review", END)

    return graph.compile()
