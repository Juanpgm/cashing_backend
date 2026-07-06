"""Agent state — typed dictionary for LangGraph graph."""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

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

    # Google Workspace — evidencias de correo
    # Contexto del contrato para construir queries de búsqueda
    contrato_contexto: dict[str, str | int | float | None] | None
    obligaciones_contexto: list[dict[str, str | int | None]] | None

    # Resultados de búsqueda en Gmail
    email_query: str | None
    email_evidence: list[dict[str, str]] | None  # lista de dicts parseados del LLM
    email_message_ids: list[str] | None

    # Evidencias crudas por fuente (consumidas por evidence_orchestrator)
    email_evidencias: list[dict[str, Any]] | None
    drive_evidencias: list[dict[str, Any]] | None
    calendar_evidencias: list[dict[str, Any]] | None

    # Justificaciones generadas por obligación (evidence_justify)
    justificaciones: list[dict[str, Any]] | None

    # Estado de envío de cuenta de cobro
    email_sent: bool | None
    email_sent_id: str | None

    # Google Drive
    drive_folder_id: str | None
    drive_file_ids: list[str] | None
    drive_share_links: list[str] | None

    # EXTRACT_OBLIGATIONS — input
    texto_contrato: str | None
    contrato_id_str: str | None       # UUID stringificado; None = extraer metadatos + obligaciones
    document_bytes: bytes | None      # bytes crudos (no checkpointeable si se usa persistence)
    document_filename: str | None

    # EXTRACT_OBLIGATIONS — output
    contrato_extraido: dict[str, str | int | float | None] | None
    obligaciones_extraidas: list[dict[str, str | int]] | None
    extraction_avisos: list[str] | None

    # GENERATE_ACTIVITIES — input
    cuenta_cobro_id_str: str | None
    mes: int | None
    anio: int | None

    # GENERATE_ACTIVITIES — output
    actividades_generadas: list[dict[str, str | int]] | None

    # Service-injected runtime dependencies — NOT checkpointable, never serialize
    _db: Any  # AsyncSession passed by google_workspace_service / integraciones endpoint
    _pdf_bytes: Any  # bytes of the PDF to upload (drive_upload_node)
    _pdf_filename: Any  # filename for the Drive upload

    # ── Phase 0: run tracking ─────────────────────────────────────────────
    agent_run_id: uuid.UUID | None
    current_phase: str | None           # human-readable current phase name
    quality_scores: dict[str, float] | None  # node_name → score

    # ── Phase 1: SECOP discovery & onboarding ────────────────────────────
    cedula: str | None
    secop_contratos: list[dict[str, str | int | float | None]] | None
    secop_documentos: list[dict[str, str | int | float | None]] | None
    uploaded_file_ids: list[uuid.UUID] | None
    onboarding_mode: str | None         # "secop" | "manual"

    # ── Phase 2: entity requirements & templates ──────────────────────────
    entity_requirements: dict[str, Any] | None   # EntityRequirements serialised
    entity_profile_id: uuid.UUID | None
    entity_type: str | None                      # e.g. "sena", "alcaldia", "ministerio"
    template_id: uuid.UUID | None
    document_type: str | None           # "cuenta_cobro" | "informe_actividades" | "anexo"
    hil_reason: str | None              # Reason the agent paused for human review

    # ── Phase 3: quality gate ─────────────────────────────────────────────
    quality_gate_passed: bool | None
    quality_issues: list[str] | None

    # ── Phase 4: evidence orchestration ──────────────────────────────────
    evidence_raw: list[dict[str, Any]] | None
    local_evidence: list[dict[str, Any]] | None
    matched_evidence: dict[str, list[dict[str, Any]]] | None  # obligacion_id → evidencias
    deduplicated_evidence: list[dict[str, Any]] | None
    evidencias_descartadas: int | None  # items filtrados como ruido por evidence_filter

    # ── Phase 5: document assembly ────────────────────────────────────────
    document_drafts: list[dict[str, Any]] | None
    preview_html: str | None
    preview_approved: bool | None
    folder_manifest: dict[str, str] | None      # doc_type → S3/Drive path

    # ── Phase 6: supervisor & HIL ─────────────────────────────────────────
    supervisor_plan: list[str] | None           # ordered list of nodes to execute
    borrador_version: int | None
    human_review_pending: bool | None
    hil_feedback: str | None                    # injected by resume() before graph restart
