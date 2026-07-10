# CashIn Backend

AI Agent-first backend for automating Colombian contractor billing (cuentas de cobro).

**Cloud-agnostic** — MVP deployed on **Railway**. No AWS/GCP/Azure dependency.
Ports & Adapters architecture enables future cloud migration without touching core business logic.

## Stack

> ⚠️ Historical/aspirational — the implemented architecture differs; see CLAUDE.md (custom CompiledGraph engine, no LangGraph/CrewAI). In particular: no LangGraph/CrewAI/langgraph-checkpoint dependency (a custom `CompiledGraph` engine in `app/agent/engine.py` is used instead), pgvector is used where available but SQLite dev/test runs fall back without it, and Langfuse is optional/inactive unless `LANGFUSE_*` keys are configured.

| Layer          | Technology                        | Purpose                                           |
| -------------- | --------------------------------- | ------------------------------------------------- |
| Runtime        | Python 3.12 + FastAPI             | Async API framework                               |
| AI Engine      | Custom `CompiledGraph` + LiteLLM  | Agent workflows + LLM abstraction (100+ models)   |
| Local LLM      | Ollama                            | Zero-cost local models for dev/privacy            |
| MCP            | mcp[cli] (Anthropic SDK)          | Agent tool servers (Gmail, Drive, Calendar)       |
| Google APIs    | google-api-python-client          | Gmail, Drive, Calendar integration                |
| Database       | PostgreSQL 16 + pgvector (optional) | Primary data + semantic search on embeddings    |
| Storage        | Cloudflare R2 / MinIO             | S3-compatible object storage                      |
| Document gen   | WeasyPrint + python-docx + Jinja2 | PDF and DOCX generation from templates            |
| OCR            | pytesseract + pdf2image           | Scanned document text extraction                  |
| SECOP          | Socrata API (data.gov.co)         | SECOP II public contracts (datasets jbjy-vk9h, p6dx-8zbt) |
| Observability  | Langfuse (self-hosted, optional)  | LLM trace logging, quality scores, cost tracking  |
| Payments       | Wompi                             | Colombian payment gateway                         |
| Deploy         | Railway                           | Docker container (zero-config CI/CD)              |

## Quick Start

```bash
# 1. Setup environment (uv + deps + pre-commit)
make setup

# 2. Start local services (PostgreSQL + MinIO + Redis)
make up

# 3. Run database migrations
make migrate

# 4. Start development server (hot-reload on :8000)
make dev

# Open http://localhost:8000/docs for API documentation
```

## Commands

| Command                    | Description                             |
| -------------------------- | --------------------------------------- |
| `make setup`               | Install deps with uv + pre-commit hooks |
| `make dev`                 | Dev server with hot-reload (:8000)      |
| `make up` / `make down`    | Start/stop Docker services              |
| `make migrate`             | Apply Alembic migrations                |
| `make migration msg="..."` | Generate new migration                  |
| `make test`                | Run pytest suite                        |
| `make test-cov`            | Tests + HTML coverage report (70% min)  |
| `make lint`                | Ruff check + mypy strict                |
| `make format`              | Auto-fix style with Ruff                |
| `make security`            | Bandit + pip-audit                      |
| `make e2e`                 | Playwright E2E tests (Phase 8)          |
| `make load-test`           | Locust load tests                       |

## Architecture

```
HTTP Request → FastAPI (api/v1/) → Service Layer → Agent Engine (CompiledGraph) → LLM + DB + Storage
```

### Ports & Adapters (cloud-agnostic)

```
app/adapters/
├── llm/
│   ├── port.py              # LLMPort Protocol
│   └── litellm_adapter.py   # Gemini → Groq → Ollama (fallback chain)
├── storage/
│   ├── port.py              # StoragePort Protocol
│   └── s3_adapter.py        # MinIO (dev) / R2 (prod) / any S3-compatible
├── email/
│   ├── port.py              # EmailPort Protocol
│   └── gmail_adapter.py     # Gmail API (OAuth 2.0 + Fernet-encrypted tokens)
├── drive/
│   ├── port.py              # DrivePort Protocol
│   └── drive_adapter.py     # Google Drive API
└── calendar/
    ├── port.py              # CalendarPort Protocol
    └── gcal_adapter.py      # Google Calendar API
```

The core (`services/`, `agent/`, `models/`) **never** imports cloud SDKs directly.

### MCP Servers

```
mcp_servers/
├── gmail_server.py    # Tools: search_emails, get_email, send_email
├── drive_server.py    # Tools: upload_file, list_files, create_folder
└── calendar_server.py # Tools: list_events, get_event
# + filesystem_server.py (Phase 7)
```

Register in `mcp_servers/mcp_config.json` to use with Claude Code.

## Project Structure

```
app/
├── api/        # FastAPI routes (/api/v1/)
├── agent/      # Custom CompiledGraph AI agent (nodes, prompts, tools)
├── models/     # SQLAlchemy models (18+ domain models)
├── schemas/    # Pydantic v2 schemas
├── services/   # Business logic layer
├── adapters/   # Ports & Adapters (storage, LLM, email, drive)
├── core/       # Config, DB, security, exceptions, middleware
└── main.py     # FastAPI app entry point
```

## Environment

Copy `.env.example` → `.env` and configure:

- `DATABASE_URL` — PostgreSQL connection string
- `JWT_SECRET_KEY` — Min 32 chars (use `scripts/generate_secrets.py`)
- `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` — Storage
- `GEMINI_API_KEY` / `GROQ_API_KEY` — LLM providers (primary: Gemini Flash, routing: Groq)
- `WOMPI_*` keys — Payment gateway (optional for dev)
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` — Observability (optional for dev)

## Deploy (Railway)

Push to GitHub → Railway auto-deploys via `Dockerfile` (`railway.toml` sets `builder = "DOCKERFILE"` —
there is no separate Procfile; the Dockerfile `CMD` is the single source of truth for the start command).

- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1` (single worker is intentional —
  see below)
- Health check: `GET /health`
- PostgreSQL managed by Railway
- **Migrations/schema run in-process at startup**, inside the FastAPI `lifespan` handler
  (`app/main.py`): `Base.metadata.create_all` builds the base schema (idempotent, source of truth),
  then Alembic is invoked as a subprocess to either `stamp head` (fresh create_all-built DB) or
  `upgrade head` (apply pending ALTER deltas on an already-versioned DB). There is no separate
  release/migration step in the deploy pipeline.
- **Single worker is intentional**: with migrations running in-process at startup, multiple workers
  would race to run `create_all`/`alembic stamp-or-upgrade` concurrently on boot. Scale out via
  Railway replicas (separate instances, each running its own lifespan) rather than `--workers N`
  in-process, or extract the migration step into a dedicated release phase before increasing workers.

## License

Proprietary — All rights reserved.
