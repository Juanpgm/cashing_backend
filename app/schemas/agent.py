"""Agent schemas — request/response models for chat and document processing."""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- LLM schemas ---


class LLMMessage(BaseModel):
    role: str = Field(description="Role: system, user, assistant, or tool")
    # Plain text for normal completions, or a list of multimodal content parts
    # (e.g. {"type": "text", ...} + {"type": "image_url"/"file", ...}) for vision input.
    # Empty string is valid for an assistant message that only carries tool_calls.
    content: str | list[dict[str, Any]] = ""
    # Present on role="tool" messages: the id of the LLMToolCall this message answers.
    tool_call_id: str | None = None
    # Present on role="assistant" messages that requested tool calls (OpenAI shape,
    # as returned by litellm — list of {"id", "type", "function": {"name", "arguments"}}).
    tool_calls: list[dict[str, Any]] | None = None


class LLMToolCall(BaseModel):
    """A single tool call requested by the model (parsed from litellm's response)."""

    id: str
    name: str
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    tool_calls: list[LLMToolCall] | None = None


# --- Chat schemas ---


class ChatMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    session_id: uuid.UUID | None = None


class ChatMessageResponse(BaseModel):
    session_id: uuid.UUID
    role: str = "assistant"
    content: str
    tokens_used: int = 0


class ConversationHistoryResponse(BaseModel):
    session_id: uuid.UUID
    messages: list[dict[str, str]]

    model_config = {"from_attributes": True}


# --- Document schemas ---


class ObligacionExtraida(BaseModel):
    descripcion: str
    tipo: str  # "general" | "especifica"
    orden: int
    etiqueta: str = ""  # original marker from the contract (e.g. "A", "1", "a", "iii")


class ContratoExtraido(BaseModel):
    """Datos del contrato extraídos automáticamente por LLM desde el texto del documento."""

    numero_contrato: str
    objeto: str
    valor_total: Decimal = Field(default=Decimal("0.00"))
    valor_mensual: Decimal = Field(default=Decimal("0.00"))
    fecha_inicio: date | None = None
    fecha_fin: date | None = None
    supervisor_nombre: str | None = None
    cargo_supervisor: str | None = None
    entidad: str | None = None
    dependencia: str | None = None
    documento_proveedor: str | None = None
    pais: str | None = None
    departamento: str | None = None
    ciudad: str | None = None
    direccion_ejecucion: str | None = None


# --- Structured LLM extraction schemas (response_format / JSON) ---


class ContratoCamposLLM(BaseModel):
    """Raw structured contract metadata returned by the LLM (response_format).

    All fields are strings so the JSON schema stays simple for the model.
    Numeric/date conversion happens downstream via the document_service safe
    parsers (``_safe_decimal`` / ``_safe_date``). An empty string means
    "not found in the document".
    """

    numero_contrato: str = ""
    objeto: str = ""
    valor_total: str = ""
    valor_mensual: str = ""
    fecha_inicio: str = ""
    fecha_fin: str = ""
    supervisor_nombre: str = ""
    cargo_supervisor: str = ""
    entidad: str = ""
    dependencia: str = ""
    documento_proveedor: str = ""
    pais: str = ""
    departamento: str = ""
    ciudad: str = ""
    direccion_ejecucion: str = ""


class ObligacionItemLLM(BaseModel):
    """A single obligation item in a structured LLM response."""

    descripcion: str
    tipo: str = "especifica"
    etiqueta: str = ""  # original marker from the contract (e.g. "A", "1", "a")


class ObligacionesLLMList(BaseModel):
    """Top-level wrapper for a structured list of obligations (response_format)."""

    obligaciones: list[ObligacionItemLLM] = Field(default_factory=list)


class ContratoExtractionResult(BaseModel):
    """Combined structured result from the multimodal (vision) extraction path.

    Carries the contract metadata, its specific obligations, and a plain-text
    transcription of the document so ``texto_extraido`` can be populated for a
    scanned PDF or image (the vision model acts as the OCR).
    """

    contrato: ContratoCamposLLM = Field(default_factory=ContratoCamposLLM)
    obligaciones: list[ObligacionItemLLM] = Field(default_factory=list)
    transcripcion: str = ""


class DocumentUploadResponse(BaseModel):
    id: uuid.UUID
    nombre: str
    tipo: str
    texto_extraido: str | None = None
    contrato_id: uuid.UUID | None = Field(
        default=None,
        description="UUID del contrato asociado (existente o auto-creado)",
    )
    contrato_creado: ContratoExtraido | None = Field(
        default=None,
        description="Datos del contrato creado automáticamente (solo cuando tipo=contrato y no se pasó contrato_id)",
    )
    obligaciones_extraidas: list[ObligacionExtraida] = Field(
        default_factory=list,
        description="Obligaciones detectadas automáticamente del contrato (solo cuando tipo=contrato)",
    )
    avisos: list[str] = Field(
        default_factory=list,
        description="Advertencias o errores durante la extracción automática (LLM, parsing, etc.)",
    )

    model_config = {"from_attributes": True}


class ObligacionesExtraerResponse(BaseModel):
    contrato_id: uuid.UUID
    obligaciones: list[ObligacionExtraida]
    total: int
    avisos: list[str] = Field(default_factory=list)


class DocumentProcessRequest(BaseModel):
    document_id: uuid.UUID


class DocumentProcessResponse(BaseModel):
    document_id: uuid.UUID
    texto_extraido: str
    metadata: dict[str, str | int | float | None] | None = None


# --- Free-form agent chat (tool-calling) schemas ---
#
# Response contract for POST /api/v1/agent/chat — see `app.services.agent_chat_service`.
# FROZEN: the frontend is built against this exact JSON shape, keep field names stable.


class ToolEvent(BaseModel):
    """One tool invocation performed by the agent loop while answering a message."""

    tool: str
    status: Literal["ok", "error"]
    resumen: str


class DocumentoAdjuntoResumen(BaseModel):
    """Summary of one file attachment processed during the chat turn."""

    filename: str
    caracteres_extraidos: int = Field(description="Characters of text extracted from this attachment (0 if binary).")


class AgentChatResult(BaseModel):
    session_id: str
    content: str
    tool_events: list[ToolEvent] = Field(default_factory=list)
    documentos: list[DocumentoAdjuntoResumen] = Field(default_factory=list)
    tokens_used: int = 0


# --- Agent state schemas ---


class AgentMode(StrEnum):
    CHAT = "chat"
    PIPELINE = "pipeline"
    CONFIG = "config"
    EVIDENCE = "evidence"
    DRIVE = "drive"
    EXTRACT_OBLIGATIONS = "extract_obligations"
    GENERATE_ACTIVITIES = "generate_activities"
    # Phases 1-6 (implemented incrementally per plan)
    SECOP_DISCOVERY = "secop_discovery"
    REQUIREMENTS_INGESTION = "requirements_ingestion"
    TEMPLATE_RESOLVE = "template_resolve"
    QUALITY_GATE = "quality_gate"
    CUENTA_COBRO_FULL = "cuenta_cobro_full"
