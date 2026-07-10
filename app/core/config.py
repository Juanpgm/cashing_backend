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

    # MCP server — mounts the curated tool registry (app.tools.registry.TOOL_REGISTRY)
    # at /mcp via app.mcp.server. Never exposes auth/payments/credits — see app/tools/catalog.
    # Off by default (pilot hardening): opt in explicitly via .env once the surface
    # has been reviewed for the deployment target, rather than exposing it by default.
    MCP_ENABLED: bool = False

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
    # STORAGE_PROVIDER: "local" (default dev, no MinIO needed) | "minio" | "s3"
    STORAGE_PROVIDER: str = "local"
    # Filesystem root for STORAGE_PROVIDER=local (relative to cwd or absolute)
    LOCAL_STORAGE_PATH: str = "local_storage"
    S3_ENDPOINT_URL: str | None = None  # None → native AWS S3; set URL for MinIO/R2
    S3_ACCESS_KEY: str = "minioadmin"
    S3_SECRET_KEY: str = "minioadmin"
    S3_REGION: str = "us-east-1"
    S3_BUCKET_EVIDENCIAS: str = "cashin-evidencias"
    S3_BUCKET_DOCUMENTOS: str = "cashin-documentos"
    S3_BUCKET_PDFS: str = "cashin-pdfs"
    S3_BUCKET_AVATARS: str = "cashin-avatars"

    # LLM — Groq for fast chat/routing; Gemini 2.5 Flash for document extraction (generous free tier)
    # Gemini free tier: 1,000,000 TPM/day vs Groq 8b: ~20,000 TPM/day
    # Note: gemini-2.0-flash and gemini-1.5-flash are deprecated for new accounts — use gemini-2.5-flash
    LLM_DEFAULT_MODEL: str = "ollama/llama3.1:8b"
    LLM_FALLBACK_MODEL: str = "ollama/llama3.1:8b"
    LLM_LOCAL_MODEL: str = "ollama/llama3.1:8b"
    # LLM_EXTRACTION_MODEL: dedicated model for document/obligation extraction.
    # Gemini 2.5 Flash-Lite: 1M TPM/day, 1,500 RPD free — cheapest option for text extraction.
    # Switch to "gemini/gemini-2.5-flash" for heavier reasoning on complex contracts.
    LLM_EXTRACTION_MODEL: str = "gemini/gemini-2.5-flash-lite"
    # Multimodal (vision) fallback for scanned PDFs and images — the model reads
    # the file directly and acts as the OCR. Used only when text extraction yields
    # fewer than EXTRACTION_MIN_TEXT_CHARS characters.
    #   Free tier options (no billing required):
    #     "gemini/gemini-2.5-flash-lite"                        — 30 RPM/1,500 RPD free; reads PDF natively
    #     "gemini/gemini-2.5-flash"                             — 15 RPM/1,500 RPD free; reads PDF natively
    #     "mistral/pixtral-12b-2409"                            — 1B tokens/month free; rasterizes PDF (no page limit)
    #     "groq/meta-llama/llama-4-scout-17b-16e-instruct"      — free RPM limits; rasterizes PDF; MAX 5 pages
    #   Local (offline, free):
    #     "ollama/llama3.2-vision" — rasterizes PDF to images (Ollama doesn't read PDF natively)
    # Resilience: the service tries this model first, then curated current fallbacks
    # (see _VISION_FALLBACK_MODELS in document_service). A decommissioned or
    # quota-exhausted model falls through instead of breaking extraction entirely.
    LLM_MULTIMODAL_MODEL: str = "gemini/gemini-2.5-flash-lite"
    EXTRACTION_MULTIMODAL_FALLBACK_ENABLED: bool = True
    EXTRACTION_MIN_TEXT_CHARS: int = 200
    # When a PDF is rasterized (for a local vision model or the OCR tier), cap
    # pages and resolution to keep payloads and latency reasonable on a dev machine.
    MULTIMODAL_MAX_PDF_PAGES: int = 8
    MULTIMODAL_RASTER_DPI: int = 150
    # OCR tier — runs BEFORE the vision model for scanned PDFs/images: rasterizes
    # the document and reads it with a local OCR engine (free, fast, no LLM); the
    # deterministic extractor then runs on the recovered text. The vision model is
    # used only if OCR text is insufficient. Set EXTRACTION_OCR_ENABLED=false to
    # skip OCR and go straight to vision.
    EXTRACTION_OCR_ENABLED: bool = True
    # OCR engine: "rapidocr" (default, pip-only, no system binary) or "tesseract" (needs binary + tessdata).
    # rapidocr deps: rapidocr-onnxruntime + opencv-python-headless + onnxruntime + pyclipper + shapely
    # tesseract deps: Tesseract binary (apt install tesseract-ocr tesseract-ocr-spa) + pytesseract pip wrapper
    EXTRACTION_OCR_ENGINE: str = "rapidocr"
    EXTRACTION_OCR_LANG: str = "spa"  # Tesseract only — ignored for rapidocr (auto-detects language)
    # Absolute path to the Tesseract binary. Leave empty when it is on PATH; set it
    # on Windows if the installer did not add it, e.g.
    # "C:/Program Files/Tesseract-OCR/tesseract.exe".
    TESSERACT_CMD: str = ""
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""
    MISTRAL_API_KEY: str = ""
    # LLM_PRODUCTION_FALLBACK_MODEL: used in production instead of LLM_LOCAL_MODEL (Ollama).
    # Set to e.g. "gemini/gemini-2.0-flash" and configure GEMINI_API_KEY.
    # When empty and is_production=True, the Ollama slot is silently dropped.
    LLM_PRODUCTION_FALLBACK_MODEL: str = ""
    OPENAI_API_KEY: str = ""

    # Firebase Admin SDK
    # Local dev: set FIREBASE_SERVICE_ACCOUNT_PATH to the JSON file path (e.g. secrets/firebase-service-account.json)
    # Production: set FIREBASE_SERVICE_ACCOUNT_JSON to the minified JSON string in the platform dashboard
    FIREBASE_SERVICE_ACCOUNT_PATH: str = ""
    FIREBASE_SERVICE_ACCOUNT_JSON: str = ""

    # Google OAuth + Workspace
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/integraciones/google/callback"
    # Scopes "explorer": lectura amplia para descubrir evidencias en Gmail, Drive y Calendar.
    # - gmail.readonly: leer correos como evidencia; gmail.send/compose: enviar la cuenta de cobro.
    # - drive.readonly: explorar TODO el Drive del usuario (no solo archivos creados por la app).
    # - calendar.readonly: leer eventos (reuniones, entregas) como evidencia.
    # Nota: drive.readonly y gmail.readonly son scopes "restringidos" y requieren verificación
    # de Google para producción; en local funcionan con usuarios de prueba (modo Testing).
    GOOGLE_OAUTH_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.compose",
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/calendar.readonly",
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    ]

    # Local dev: skip OAuth client setup and use `gcloud auth application-default login` instead.
    # Never set this in production.
    GOOGLE_USE_ADC: bool = False

    # Frontend base URL — used to redirect the browser back after the OAuth callback.
    FRONTEND_URL: str = "http://localhost:3000"
    # Signed OAuth state token TTL — covers the Google round-trip window.
    GOOGLE_OAUTH_STATE_TTL_SECONDS: int = 600

    # Wompi
    WOMPI_PUBLIC_KEY: str = "pub_test_xxx"
    WOMPI_PRIVATE_KEY: str = "prv_test_xxx"
    WOMPI_EVENTS_SECRET: str = "test_events_xxx"
    WOMPI_API_URL: str = "https://sandbox.wompi.co/v1"

    # Encryption
    TOKEN_ENCRYPTION_KEY: str = "your-fernet-key-here"

    # SECOP — datos.gov.co public contracting API
    SECOP_APP_TOKEN: str = ""

    # SECOP II scraper microservice (Playwright-based). Used by "agentic" mode
    # to fetch contract-phase documents not available in datos.gov.co.
    SECOP_SCRAPER_URL: str = ""
    SECOP_SCRAPER_INTERNAL_TOKEN: str = ""
    # Sliding-window quota for the manual "Exploración Agéntica" trigger.
    SECOP_AGENTIC_HOURLY_LIMIT: int = 20

    # Langfuse — LLM observability (Phase 7)
    # Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY to enable tracing.
    LANGFUSE_PUBLIC_KEY: str = ""
    LANGFUSE_SECRET_KEY: str = ""
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # Credits
    CREDITS_PER_CUENTA_COBRO: int = 10
    CREDITS_PER_CHAT_MESSAGE: int = 1
    CREDITS_PER_EVIDENCE_COLLECTION: int = 5
    FREE_CREDITS_ON_SIGNUP: int = 30

    # Waitlist / invite-code gate: when True, account creation (email + first-time
    # Google sign-in) requires a valid, active, non-exhausted invite code.
    WAITLIST_ENABLED: bool = False

    # Outbound notifications: pluggable channel, off by default. Fail-open — a
    # delivery failure never breaks the triggering operation.
    NOTIFICATIONS_ENABLED: bool = False
    NOTIFICATION_CHANNEL: str = "log"  # "log" | "webhook"
    NOTIFICATION_WEBHOOK_URL: str = ""

    # PDF digital signature (PAdES). Off by default. Without CERT/KEY paths an
    # ephemeral self-signed cert is used (technically valid, NO legal validity).
    # For legal validity in Colombia, point these at a cert from an accredited
    # entity (e.g. Certicámara, Andes SCD).
    PDF_SIGNATURE_ENABLED: bool = False
    PDF_SIGNATURE_CERT_PATH: str = ""
    PDF_SIGNATURE_KEY_PATH: str = ""
    PDF_SIGNATURE_KEY_PASSPHRASE: str = ""

    # Evidence discovery — "maximum effort" caps. These replace hardcoded slices
    # in evidence_discovery_service / drive_fetch / calendar_fetch / evidence_matcher
    # so the fan-out can be tuned per environment instead of silently truncating.
    # 0 = no cap (process ALL obligaciones).
    EVIDENCE_MAX_OBLIGACIONES_QUERIES: int = 0
    EVIDENCE_QUERIES_PER_OBLIGACION: int = 3
    EVIDENCE_MAX_QUERIES_TOTAL: int = 24
    EVIDENCE_MAX_EMAILS_TOTAL: int = 60
    EVIDENCE_MAX_FILES_TOTAL: int = 60
    EVIDENCE_MAX_EVENTS: int = 100
    EVIDENCE_MATCHER_TOP_N: int = 8

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
