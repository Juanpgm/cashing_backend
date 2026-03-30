"""Agent schemas — request/response models for chat and document processing."""

import uuid
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


class DocumentUploadResponse(BaseModel):
    id: uuid.UUID
    nombre: str
    tipo: str
    texto_extraido: str | None = None

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
