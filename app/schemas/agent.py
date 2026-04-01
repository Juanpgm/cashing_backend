"""Agent schemas — request/response models for chat and document processing."""

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

# --- LLM schemas ---


class LLMMessage(BaseModel):
    role: str = Field(description="Role: system, user, or assistant")
    content: str


class LLMResponse(BaseModel):
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


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


class ContratoExtraido(BaseModel):
    """Datos del contrato extraídos automáticamente por LLM desde el texto del documento."""

    numero_contrato: str
    objeto: str
    valor_total: Decimal = Field(default=Decimal("0.00"))
    valor_mensual: Decimal = Field(default=Decimal("0.00"))
    fecha_inicio: date | None = None
    fecha_fin: date | None = None
    supervisor_nombre: str | None = None
    entidad: str | None = None
    dependencia: str | None = None
    documento_proveedor: str | None = None


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


class DocumentProcessRequest(BaseModel):
    document_id: uuid.UUID


class DocumentProcessResponse(BaseModel):
    document_id: uuid.UUID
    texto_extraido: str
    metadata: dict[str, str | int | float | None] | None = None


# --- Agent state schemas ---


class AgentMode(StrEnum):
    CHAT = "chat"
    PIPELINE = "pipeline"
    CONFIG = "config"
