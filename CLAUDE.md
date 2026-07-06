# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Package manager:** `uv`

```bash
make setup          # Install dependencies & pre-commit hooks
make dev            # Start dev server with hot-reload on localhost:8000
make up / make down # Start/stop Docker Compose (PostgreSQL, MinIO, Redis)
make migrate        # Apply Alembic migrations (alembic upgrade head)
make migration msg="description"  # Auto-generate a new migration
make test           # Run pytest suite
make test-cov       # Run tests with HTML coverage report (70% threshold required)
make lint           # Ruff check + format check + mypy strict
make format         # Auto-fix code style with Ruff
make security       # Bandit + pip-audit vulnerability scan
```

**Run a single test:**

```bash
uv run pytest tests/path/to/test_file.py::test_name -v
```

## Architecture

**Stack:** FastAPI + SQLAlchemy 2.0 async (asyncpg) + PostgreSQL 16 + custom async graph engine (`CompiledGraph`) + LiteLLM + MCP

> **Note:** The agent engine is a custom async graph runner at `app/agent/engine.py` (`CompiledGraph`, `END`, `HumanInterrupt`). It mirrors LangGraph's `StateGraph` builder API (`add_node`, `add_edge`, `add_conditional_edges`, `set_entry_point`, `compile`, `ainvoke`) but does NOT depend on LangGraph — `langgraph` is not imported anywhere and is absent from `requirements.txt`. Older planning docs that mention LangGraph are historical.

The application is an AI-powered backend for automating Colombian contractor billing ("cuentas de cobro") via agent-driven workflows and MCP-based integrations.

### Request Flow

```
HTTP Request → FastAPI (api/v1/) → Service Layer → Agent Engine (CompiledGraph) → LLM (via LiteLLM) + DB + Storage
MCP Client   → MCP Servers (mcp_servers/) → Adapters (email/drive/calendar) → Google APIs
```

For chat/document endpoints, the core execution path is:

1. `api/v1/chat.py` or `api/v1/documentos.py` receives the request
2. `services/agent_service.py` loads/creates conversation state and invokes the compiled agent graph (custom engine, `agent/engine.py`)
3. The **router node** (`agent/nodes/router.py`) classifies intent (chat / pipeline / config / evidence)
4. Either the **chat node**, **pipeline nodes**, or **evidence node** execute
5. Responses stream back or return as JSON with token usage

### Agent Graph Workflow

```
Input → [router] → chat mode     → [chat node] → END
                 → pipeline mode  → [doc_ingestion] → [doc_understanding] → [classification] → [justification] → END
                 → evidence mode  → [email_fetch] → [obligation_matching] → [justification] → END
                 → drive mode     → [drive_upload] → END
```

State is typed via `AgentState` (`agent/state.py`) — a TypedDict with `total=False`. Nodes return partial state updates (spread pattern: `{**state, "key": value}`).

### Key Abstractions (Ports & Adapters)

- **`adapters/llm/port.py`** — `LLMPort` with `complete()` and `stream()`. Implementation: `LiteLLMAdapter` (Gemini → Groq → Ollama fallback chain).
- **`adapters/storage/port.py`** — `StoragePort` for file operations. Implementation: `S3Adapter` (MinIO dev, Cloudflare R2 prod).
- **`adapters/email/port.py`** — `EmailPort` with `search_messages()`, `send_message()`. Implementation: `GmailAdapter` (Google API + Fernet-encrypted tokens).
- **`adapters/drive/port.py`** — `DrivePort` with `upload_file()`, `get_or_create_folder()`, `make_shareable()`. Implementation: `DriveAdapter`.
- **`adapters/calendar/port.py`** — `CalendarPort` with `list_events()`. Implementation: `GoogleCalendarAdapter`.

All injected via FastAPI's dependency system (`api/deps.py`).

### MCP Servers

Standalone Python processes in `mcp_servers/` expose agent tools to Claude Code and other MCP clients. They proxy requests to the FastAPI backend (auth-centralized):

```
mcp_servers/
├── gmail_server.py    # Tools: search_emails, get_email, send_email
├── drive_server.py    # Tools: upload_file, list_files, create_folder
└── calendar_server.py # Tools: list_events, get_event
```

Register in `.claude/settings.json`:
```json
{
  "mcpServers": {
    "gmail": { "command": "uv", "args": ["run", "python", "mcp_servers/gmail_server.py"] },
    "drive": { "command": "uv", "args": ["run", "python", "mcp_servers/drive_server.py"] }
  }
}
```

### Database Models

All models inherit from `Base` with mixins in `models/base.py`:

- `UUIDMixin` — UUID primary key (uuid4)
- `TimestampMixin` — `created_at` / `updated_at` with server defaults
- `SoftDeleteMixin` — logical deletes via `deleted_at`

Core domain models: `CuentaCobro` (invoice), `Contrato` (contract), `Conversacion` (chat session), `Actividad`, `Obligacion`, `Evidencia`, `Pago`, `Suscripcion`, `Credito`.

Conversation history is stored as JSON (`mensajes_json` column) in the `Conversacion` model.

### Error Handling

Domain exceptions live in `core/exceptions.py` (`NotFoundError`, `AlreadyExistsError`, `InsufficientCreditsError`, etc.). They map to HTTP status codes via `domain_to_http()`. Raise domain exceptions in services; the exception handlers convert them automatically.

### Configuration

Pydantic Settings (`core/config.py`) loads from `.env` (see `.env.example` for all variables). Key categories: database URL, JWT secrets, S3 credentials, LLM API keys (Gemini/OpenAI), Google OAuth, Wompi payment keys, and per-action credit costs.

### Middleware Stack (order is significant)

`SecurityHeadersMiddleware` → `AuditMiddleware` → `CORSMiddleware` → slowapi rate limiter

### Testing Conventions

- Tests use `aiosqlite` as the in-memory test DB (not PostgreSQL)
- S3 calls are mocked with `moto[s3]`
- Use `factory-boy` factories for test data
- `pytest-asyncio` with `asyncio_mode = "auto"` — no need to mark async tests

### Deployment

MVP deployed on **Railway** (Docker container). No dependency on AWS, GCP, or Azure.

- Railway provides managed PostgreSQL and zero-config CI/CD from GitHub push
- Storage: Cloudflare R2 (prod), MinIO (dev) — both S3-compatible
- Cloud provider migration requires only writing new adapters in `app/adapters/` — the core never imports cloud SDKs

## Implementation Order

For every new feature, follow this sequence: `model → schema → service → api → test`

1. Define/update Pydantic schemas in `app/schemas/`
2. Implement service in `app/services/` (raises domain exceptions, never `HTTPException`)
3. Expose endpoint in `app/api/v1/` (delegates all logic to service — no SQL in routers)
4. Inject dependencies via `app/api/deps.py` (`get_db`, `get_current_user`, `get_storage`, `get_llm`)
5. Add unit tests (services) + integration tests (API with `httpx.AsyncClient`)

## Anti-Patterns (never do these)

```python
# ❌ Raw SQL → use SQLAlchemy ORM
# ❌ HTTPException in service layer → use domain exceptions (core/exceptions.py)
# ❌ Sync database calls → always AsyncSession
# ❌ Import boto3 in services → use StoragePort
# ❌ Import litellm/openai in agent nodes → use LLMPort
# ❌ Call Google APIs directly in services → use EmailPort/DrivePort/CalendarPort
# ❌ Call Google APIs synchronously → wrap with run_in_executor
# ❌ Store OAuth tokens in plaintext → encrypt with Fernet before DB storage
# ❌ MCP servers connecting to Google directly → proxy through FastAPI backend
# ❌ print() → use structlog
# ❌ Hardcoded secrets → use Settings via .env
# ❌ shell=True in subprocess
# ❌ float for monetary amounts → use Decimal
# ❌ Return ORM models from endpoints → always map to schemas
```

## Naming Conventions

| Element | Convention | Example |
|---------|-----------|---------|
| DB models | Singular Spanish | `Usuario`, `Contrato`, `Obligacion` |
| Schemas | PascalCase + suffix | `LoginRequest`, `TokenResponse` |
| Services | `snake_case` functions | `create_cuenta_cobro()` |
| Constants | `UPPER_SNAKE` | `CREDITS_PER_CHAT_MESSAGE` |
| Tests | `test_` + description | `test_login_wrong_password` |
| API paths | snake_case | `/api/v1/cuentas_cobro` |

## Commit Convention

```
feat(modulo): descripcion
fix(modulo): descripcion
test(modulo): descripcion
refactor(modulo): descripcion
docs: descripcion
```

## Roadmap Status

| Phase | Name | Status |
|-------|------|--------|
| 1 | Foundations (DB, Auth, Core, Storage) | ✅ Done |
| 2 | AI Agent Engine (custom CompiledGraph, LLM, Tools) | ✅ Done |
| 3 | Contracts, Cuentas de Cobro, Templates | 🔄 In progress |
| 4 | Google Workspace + MCP Servers + Evidence | 🔄 In progress |
| 5 | Payments & Monetization (Wompi + credits) | ⬚ Pending |
| 6 | Security Hardening (continuous) | 🔄 Ongoing |
| 7 | Document Generation (DOCX/PDF templates) | ⬚ Pending |
| 8 | Additional Integrations (Outlook, Calendar, OCR) | ⬚ Pending |
| 9 | Multi-cloud deployment (GCP/AWS options) | ⬚ Pending |
| 10 | Production-Ready (cache, notifications, CI/CD) | ⬚ Pending |

## MCP Development

New capabilities follow this sequence: `adapter port → adapter impl → service → api endpoint → mcp_server tool → test`

For Google Workspace tools:
1. OAuth flow: `GET /integraciones/google/connect` → user grants scopes → `GET /integraciones/google/callback`
2. Tokens encrypted with Fernet and stored in `google_tokens` table
3. Adapters load tokens per `usuario_id`, auto-refresh on expiry
4. MCP servers proxy tool calls to the backend API (never to Google directly)

**Token encryption key generation:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Code Style

- **Line length:** 120 characters
- **Linter/formatter:** Ruff (rules: E, W, F, I, N, UP, B, S, T20, SIM, RUF)
- **Type checking:** mypy strict mode with Pydantic plugin
- **Logging:** structlog — use `structlog.get_logger("module.submodule")` per file; JSON in prod, console in dev
- Pre-commit hooks enforce secrets detection, large file checks, and Ruff formatting
- Always run `make format && make lint && make test` before committing
