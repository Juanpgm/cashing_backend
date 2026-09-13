"""Application configuration via Pydantic Settings."""

import os
from typing import Any, Literal

import structlog
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = structlog.get_logger("core.config")

# Single in-code source of truth for the running app version, referenced by both
# `app.main`'s FastAPI(version=...) (OpenAPI metadata) and
# `app.schemas.common.HealthResponse.version` (what GET /health actually returns).
# Not a Settings field on purpose — it identifies the deployed BUILD, not something
# an operator should override via env var. Still hand-kept in sync with
# pyproject.toml's [project].version: this app has no [build-system] section, so
# it is never installed as a distributable package and `importlib.metadata.version()`
# cannot resolve it. Previously drifted 3 ways (pyproject.toml, main.py, common.py)
# after a version bump silently didn't reach /health — see PR #63.
APP_VERSION = "0.2.1"


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

    # Trust reverse-proxy client-IP headers (X-Real-IP / X-Forwarded-For)?
    # See app/core/client_ip.py for the full resolution algorithm and the
    # rationale (Railway's edge proxy hides the real client IP behind its own
    # socket peer address). `None` (default) means "auto-detect": explicit
    # True/False always wins; otherwise `trust_proxy_headers_effective` treats
    # the presence of `RAILWAY_ENVIRONMENT` (an env var Railway injects into
    # every deployment) as "running on Railway" and trusts the headers, so
    # local dev stays untrusting without needing an env var and prod does not
    # depend on remembering to set one.
    TRUST_PROXY_HEADERS: bool | None = None

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

    # LLM_PROVIDER: "litellm" (default, real Gemini/Groq/Ollama chain via LiteLLMAdapter)
    # | "fake" (FakeLLMPort — deterministic, no network, no litellm import; see
    # app/adapters/llm/fake_adapter.py). Lets the RUNNING APP (not just pytest, which
    # already has its own ScriptedLLM/bloquear_red_llm test-only seam) boot and complete
    # chat turns without hitting a real model — local dev without API keys, future
    # Playwright E2E runs. The `_normalize_llm_provider` validator below case- and
    # whitespace-normalizes the raw value and folds anything other than "fake"
    # (unset, typo, invalid) to "litellm" so a bad env var falls back to the real
    # provider instead of crashing Settings load. Normalization matters here
    # specifically: without it, a typo'd casing like "FAKE" or "Fake " would
    # silently fall through to "litellm" — the REAL, network-calling provider —
    # a genuine risk in CI/E2E environments that intend to run network-free.
    LLM_PROVIDER: Literal["litellm", "fake"] = "litellm"

    @field_validator("LLM_PROVIDER", mode="before")
    @classmethod
    def _normalize_llm_provider(cls, v: Any) -> Any:
        normalized = str(v).strip().lower()
        if normalized == "fake":
            return "fake"
        if normalized != "litellm":
            _log.warning("llm_provider_invalid_value_fallback_to_litellm", received=v)
        return "litellm"

    # FAKE_LLM_SCRIPT: only read when LLM_PROVIDER=fake — selects which scripted
    # behavior `FakeLLMPort.complete()` runs (see app/adapters/llm/fake_adapter.py):
    #   None/unset (default) or "happy" -> the normal HAPPY_PATH_SEQUENCE chain.
    #   "malformed" -> one scripted tool call in the chain deliberately carries
    #       arguments that fail the tool's own input_model validation, to exercise
    #       the future Playwright `llm-respuesta-malformada` edge case end-to-end
    #       (through the REAL `invoke_tool` validation path, not a fake-only check).
    #   "stall" -> the adapter always returns a plain-text reply, never a tool
    #       call, to exercise "the agent gave up mid-chain".
    # Same case/whitespace-normalize-with-safe-fallback pattern as LLM_PROVIDER
    # above: an invalid value must never crash Settings load nor silently pick an
    # unintended script — it folds to `None` (the safe, inert happy-path default).
    FAKE_LLM_SCRIPT: Literal["happy", "malformed", "stall"] | None = None

    @field_validator("FAKE_LLM_SCRIPT", mode="before")
    @classmethod
    def _normalize_fake_llm_script(cls, v: Any) -> Any:
        if v is None:
            return None
        normalized = str(v).strip().lower()
        if normalized in ("happy", "malformed", "stall"):
            return normalized
        if normalized == "":
            return None
        _log.warning("fake_llm_script_invalid_value_fallback_to_none", received=v)
        return None

    # RATE_LIMIT_ENABLED: True (default, every environment including production
    # and CI) keeps slowapi's per-route limits (`app/core/rate_limit.py`) active,
    # e.g. `/auth/register` and `/auth/login` at 5/minute per caller IP. Set to
    # False ONLY via a local, git-ignored override (`secrets/.env.local`) when
    # running many real registrations back-to-back from the same host — e.g. a
    # local Playwright journey suite that seeds a fresh user per test/spec and
    # would otherwise legitimately trip the same per-IP bucket a real attacker
    # would. pytest's own suite does not read this: `tests/conftest.py` disables
    # the limiter unconditionally (`limiter.enabled = False`), independent of
    # this setting, and that assignment always runs after this module's import
    # so it is never overridden by a stray environment value during test runs.
    RATE_LIMIT_ENABLED: bool = True

    # LLM — Groq for fast chat/routing; Gemini 2.5 Flash for document extraction (generous free tier)
    # Gemini free tier: 1,000,000 TPM/day vs Groq 8b: ~20,000 TPM/day
    # Note: gemini-2.0-flash and gemini-1.5-flash are deprecated for new accounts — use gemini-2.5-flash
    LLM_DEFAULT_MODEL: str = "ollama/llama3.1:8b"
    LLM_FALLBACK_MODEL: str = "ollama/llama3.1:8b"
    LLM_LOCAL_MODEL: str = "ollama/llama3.1:8b"
    # LLM_EVIDENCE_CLASSIFIER_MODEL: dedicated reasoning model for the evidence
    # work/noise filter, obligation matching, and cruzar/upload relevance
    # classification (evidence_filter.py, evidence_matcher.py, cruzar_service.py,
    # evidencia_service.py). Single source of truth so a future Groq decommission
    # is a one-line/one-env-var change instead of a find-and-replace across 5
    # call sites (groq-fallback-model-decommissioned fix).
    LLM_EVIDENCE_CLASSIFIER_MODEL: str = "groq/openai/gpt-oss-20b"
    # LLM_EXTRACTION_MODEL: dedicated model for document/obligation extraction.
    # Default is gemini-2.5-flash: flash-lite returns 404 for API keys created
    # after mid-2026, so the cheaper tier is opt-in via .env only.
    LLM_EXTRACTION_MODEL: str = "gemini/gemini-2.5-flash"
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
    LLM_MULTIMODAL_MODEL: str = "gemini/gemini-2.5-flash"
    EXTRACTION_MULTIMODAL_FALLBACK_ENABLED: bool = True
    EXTRACTION_MIN_TEXT_CHARS: int = 200
    # Relaxed floor for `extraer_texto_documento(relaxed_ocr=True)` (evidence
    # classification only): a short OCR'd caption is still highly classifiable
    # evidence, so the strict spacing/length gate is bypassed above this floor.
    EVIDENCE_MIN_TEXT_CHARS: int = 20
    # When a PDF is rasterized (for a local vision model or the OCR tier), cap
    # pages and resolution to keep payloads and latency reasonable on a dev machine.
    MULTIMODAL_MAX_PDF_PAGES: int = 8
    MULTIMODAL_RASTER_DPI: int = 150
    # OCR tier — OPT-IN, disabled by default. When enabled it runs between the
    # native-text and vision tiers for scanned PDFs/images: it rasterizes the
    # document and reads it with a local OCR engine, and the deterministic
    # extractor then runs on the recovered text. It is OFF by default because on
    # a CPU-only host RapidOCR is slow (~6-12s/page) AND its concatenated output
    # usually fails the sufficiency gate, so the ladder escalates to the vision
    # model anyway — paying the OCR cost for nothing. Default flow is therefore
    # native text → vision (for CONTRATO/contract-associated docs where text is
    # genuinely needed). Set EXTRACTION_OCR_ENABLED=true to re-enable the local
    # OCR tier — intended for on-premise/local-test hosts (esp. with a GPU or
    # Tesseract) where a free local tier is preferable to per-doc vision calls.
    EXTRACTION_OCR_ENABLED: bool = False
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

    # Microsoft 365 (Azure AD) OAuth — Graph: Outlook Mail, Outlook Calendar, OneDrive.
    # App registered as multi-tenant with delegated scopes; AZURE_AD_TENANT_ID stays
    # "common" for multi-tenant + personal-account sign-in unless a specific tenant
    # is required (see openspec/changes/microsoft-365-integration/design.md D2).
    #
    # Master switch: off by default. The code is fully unit-testable (service/adapter/
    # model layers are exercised directly by tests regardless of this flag), but the
    # `/integraciones/microsoft/*` HTTP surface (connect/callback/status/revoke) stays
    # 404 until this is explicitly enabled — see `app/api/v1/integraciones.py`'s
    # `_require_ms365_enabled` guard. Never set True in production without real Azure
    # AD app credentials/consent verified end-to-end first.
    MS365_INTEGRATION_ENABLED: bool = False
    AZURE_AD_CLIENT_ID: str = ""
    AZURE_AD_CLIENT_SECRET: str = ""
    AZURE_AD_TENANT_ID: str = "common"
    AZURE_AD_REDIRECT_URI: str = "http://localhost:8000/api/v1/integraciones/microsoft/callback"
    # Delegated scopes locked by specs/microsoft-oauth/spec.md: Mail.Read, Calendars.Read,
    # Files.Read (+ offline_access for refresh tokens, User.Read for account email lookup).
    # Admin-consent-per-tenant is a deployment/doc concern, not code (tasks.md Reconciliation #2).
    # Sites.Read.All added for SharePoint team-site document libraries (search_site_files) —
    # existing connected accounts must reconnect to pick up this scope; new consents get it
    # immediately. Not covered by microsoft-oauth/spec.md; ad-hoc addition, not a locked scope.
    MICROSOFT_OAUTH_SCOPES: list[str] = [
        "Mail.Read",
        "Calendars.Read",
        "Files.Read",
        "Sites.Read.All",
        "offline_access",
        "User.Read",
    ]

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

    @field_validator("SECOP_APP_TOKEN", mode="after")
    @classmethod
    def _warn_if_secop_token_missing(cls, v: str) -> str:
        """Non-blocking: log a warning instead of raising when the token is empty.

        Socrata throttles unauthenticated requests hard, but the app must still
        start (see `secop-acquisition-resilience` spec, "Missing SECOP_APP_TOKEN
        is a warning, not a crash"). `secop_service.verificar_configuracion_secop()`
        exposes the same signal on demand for support/diagnostics.
        """
        if not v:
            _log.warning(
                "secop_app_token_missing",
                note="SECOP_APP_TOKEN is empty — Socrata will throttle; SECOP imports may be partial.",
            )
        return v

    # SECOP II scraper microservice (Playwright-based). Used by "agentic" mode
    # to fetch contract-phase documents not available in datos.gov.co.
    SECOP_SCRAPER_URL: str = ""
    SECOP_SCRAPER_INTERNAL_TOKEN: str = ""
    # Sliding-window quota for the manual "Exploración Agéntica" trigger.
    SECOP_AGENTIC_HOURLY_LIMIT: int = 20
    # Master switch for the SECOP II scraper fallback (secop-document-scraper,
    # Slice 3). Off by default: even with URL/token configured, the manual
    # trigger stays fully inert (NullSecopScraperAdapter) until explicitly
    # enabled, e.g. once the scraper microservice is verified reachable.
    SECOP_SCRAPER_ENABLED: bool = False
    # Separate, tighter quota bucket for the scraper trigger (heavier/fragile
    # than the lighter Socrata-only agentic trigger above) — design D7.
    SECOP_SCRAPER_HOURLY_LIMIT: int = 5

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

    # Discovery-result cache (radicar-ui-ux-improvements C.1): in-process TTL cache
    # keyed by (usuario_id, cuenta_id, ventana). Default ~10 min.
    DISCOVERY_CACHE_TTL_SECONDS: int = 600

    # Waitlist / invite-code gate: when True, account creation (email + first-time
    # Google sign-in) requires a valid, active, non-exhausted invite code.
    WAITLIST_ENABLED: bool = False

    # Pre-radicación coherence gate (billing-resilience-templates): when True (default),
    # `radicar_cuenta` runs `coherence_validator_service.validar_coherencia` and blocks
    # submission on any HARD finding (raises ValidationError code=COHERENCE_CHECK_FAILED).
    # Emergency disable only — SOFT findings never block regardless of this flag.
    COHERENCE_GATE_ENABLED: bool = True

    # Mandatory fail-closed secret scan before the evidence packager emits a zip
    # (billing-resilience-templates, slice #2): when True (default),
    # `informe_service.generar_zip_evidencias` scans every text-decodable package
    # member for accidentally-embedded credentials and raises
    # `SECRET_DETECTED_IN_PACKAGE` on any hit — no zip is emitted. Emergency
    # disable only: turning this off re-opens the exact credential-leak class this
    # feature exists to close.
    SECRET_SCAN_GATE_ENABLED: bool = True

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

    # Embeddings — in-memory semantic ranking signal for evidence-to-obligación
    # matching (evidence-embeddings capability). No persistent vector store:
    # vectors exist only for the duration of one classification run.
    # Groq has no embed API, so the fallback chain is embedding-capable models only.
    LLM_EMBEDDING_MODEL: str = "gemini/gemini-embedding-001"
    LLM_EMBEDDING_FALLBACK_MODEL: str = "ollama/nomic-embed-text"

    # A `running` classification job with no `updated_at` progress for this
    # long is considered stale/orphaned (crash/restart never got to mark it
    # failed) — (re)triggering resets and re-enqueues it instead of leaving it
    # stuck forever (evidence-classification-jobs: Retryable failure state,
    # RES-002 fix). A FRESH running job re-trigger is a no-op (idempotent,
    # avoids duplicate concurrent runs).
    EVIDENCE_JOB_STALE_SECONDS: int = 120

    # Confidence buckets for the blended (keyword+cosine) evidence<->obligación
    # score (evidence-obligation-links: Qualitative confidence levels). alta
    # auto-confirms the link; media/baja persist as proposed (see
    # evidencia_service.guardar_enlaces_evidencia).
    EVIDENCE_EMBED_ALTA: float = 0.75
    EVIDENCE_EMBED_MEDIA: float = 0.5

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

    @property
    def trust_proxy_headers_effective(self) -> bool:
        """Whether `app/core/client_ip.py` should trust proxy headers.

        Explicit `TRUST_PROXY_HEADERS` always wins over auto-detection.
        Otherwise: True iff `RAILWAY_ENVIRONMENT` is present in the process
        environment (Railway injects it into every deployment), else False.
        """
        if self.TRUST_PROXY_HEADERS is not None:
            return self.TRUST_PROXY_HEADERS
        return "RAILWAY_ENVIRONMENT" in os.environ


settings = Settings()
