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

**Stack:** FastAPI + SQLAlchemy 2.0 async (asyncpg) + PostgreSQL 16 + LangGraph + LiteLLM

The application is an AI-powered backend for automating Colombian contractor billing ("cuentas de cobro") via agent-driven workflows.

### Request Flow

```
HTTP Request → FastAPI (api/v1/) → Service Layer → LangGraph Agent → LLM (via LiteLLM) + DB + Storage
```

For chat/document endpoints, the core execution path is:

1. `api/v1/chat.py` or `api/v1/documentos.py` receives the request
2. `services/agent_service.py` loads/creates conversation state and invokes LangGraph
3. The **router node** (`agent/nodes/router.py`) classifies intent (chat / pipeline / config)
4. Either the **chat node** (conversational) or **pipeline nodes** (document processing) execute
5. Responses stream back or return as JSON with token usage

### LangGraph Workflow

```
Input → [router] → chat mode  → [chat node] → END
                → pipeline mode → [doc_ingestion] → [doc_understanding] → [classification] → [justification] → END
```

State is typed via `AgentState` (`agent/state.py`) — a TypedDict with `total=False`. Nodes return partial state updates (spread pattern: `{**state, "key": value}`).

### Key Abstractions (Ports & Adapters)

- **`adapters/llm/port.py`** — `LLMPort` Protocol with `complete()` and `stream()` methods. Implementation: `LiteLLMAdapter` (supports Gemini, OpenAI, Ollama with fallback chains).
- **`adapters/storage/port.py`** — `StoragePort` Protocol for file operations. Implementation: `S3Adapter` (MinIO in dev, Cloudflare R2 in prod). Cloud-agnostic: works with any S3-compatible service.

Both are injected via FastAPI's dependency system (`api/deps.py`).

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
| 2 | AI Agent Engine (LangGraph, LLM, Tools) | ✅ Done |
| 3 | Contracts, Cuentas de Cobro, Templates | 🔄 In progress |
| 4 | Google Workspace + Evidence collection | ⬚ Pending |
| 5 | Payments & Monetization (Wompi + credits) | ⬚ Pending |
| 6 | Security Hardening (continuous) | 🔄 Ongoing |
| 7 | GCP CLI Setup + OAuth | ⬚ Pending |
| 8 | Production-Ready (cache, notifications) | ⬚ Pending |

## Code Style

- **Line length:** 120 characters
- **Linter/formatter:** Ruff (rules: E, W, F, I, N, UP, B, S, T20, SIM, RUF)
- **Type checking:** mypy strict mode with Pydantic plugin
- **Logging:** structlog — use `structlog.get_logger("module.submodule")` per file; JSON in prod, console in dev
- Pre-commit hooks enforce secrets detection, large file checks, and Ruff formatting
- Always run `make format && make lint && make test` before committing
