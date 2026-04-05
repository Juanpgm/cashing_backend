"""Application configuration via Pydantic Settings."""

from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env.example", ".env", "secrets/.env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    ENVIRONMENT: str = "development"
    CORS_ORIGINS: list[str] = ["http://localhost:19006", "http://localhost:3000"]

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://cashin:password@localhost:5432/cashin"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        """Railway and some providers give postgres:// or postgresql:// — normalize to asyncpg."""
        if v.startswith("postgres://"):
            return v.replace("postgres://", "postgresql+asyncpg://", 1)
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    # JWT
    JWT_SECRET_KEY: str = "your-secret-key-min-32-chars-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Storage
    STORAGE_PROVIDER: str = "minio"
    S3_ENDPOINT_URL: str | None = None  # None → native AWS S3; set URL for MinIO/R2
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_REGION: str = "us-east-1"
    S3_BUCKET_EVIDENCIAS: str = "cashin-evidencias"
    S3_BUCKET_DOCUMENTOS: str = "cashin-documentos"
    S3_BUCKET_PDFS: str = "cashin-pdfs"

    # LLM — Groq for fast chat/routing; Gemini 2.5 Flash for document extraction (generous free tier)
    # Gemini free tier: 1,000,000 TPM/day vs Groq 8b: ~20,000 TPM/day
    # Note: gemini-2.0-flash and gemini-1.5-flash are deprecated for new accounts — use gemini-2.5-flash
    LLM_DEFAULT_MODEL: str = "groq/llama-3.1-8b-instant"
    LLM_FALLBACK_MODEL: str = "groq/llama-3.1-8b-instant"
    LLM_LOCAL_MODEL: str = "ollama/qwen2.5:7b"
    # LLM_EXTRACTION_MODEL: dedicated model for document/obligation extraction.
    # Gemini 2.5 Flash: 1M TPM/day free — ideal for large contract documents.
    LLM_EXTRACTION_MODEL: str = "gemini/gemini-2.5-flash"
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    # LLM_PRODUCTION_FALLBACK_MODEL: used in production instead of LLM_LOCAL_MODEL (Ollama).
    # Set to e.g. "gemini/gemini-2.0-flash" and configure GEMINI_API_KEY.
    # When empty and is_production=True, the Ollama slot is silently dropped.
    LLM_PRODUCTION_FALLBACK_MODEL: str = ""
    OPENAI_API_KEY: str = ""

    # Google OAuth
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/integraciones/google/callback"

    # Wompi
    WOMPI_PUBLIC_KEY: str = "pub_test_xxx"
    WOMPI_PRIVATE_KEY: str = "prv_test_xxx"
    WOMPI_EVENTS_SECRET: str = "test_events_xxx"
    WOMPI_API_URL: str = "https://sandbox.wompi.co/v1"

    # Encryption
    TOKEN_ENCRYPTION_KEY: str = "your-fernet-key-here"

    # SECOP — datos.gov.co public contracting API
    SECOP_APP_TOKEN: str = ""

    # Credits
    CREDITS_PER_CUENTA_COBRO: int = 10
    CREDITS_PER_CHAT_MESSAGE: int = 1
    CREDITS_PER_EVIDENCE_COLLECTION: int = 5
    FREE_CREDITS_ON_SIGNUP: int = 30

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            import json

            return json.loads(v)  # type: ignore[no-any-return]
        return v  # type: ignore[return-value]

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == "development"


settings = Settings()
