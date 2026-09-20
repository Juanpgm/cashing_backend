# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Resuming work from another machine or session?** Read [`docs/handoff/2026-09-19/HANDOFF.md`](docs/handoff/2026-09-19/HANDOFF.md) first: current state, ordered pending work, gotchas, what does not travel through git, and the `backup/*` branches.

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
make lock           # Regenerate uv.lock + requirements*.txt after editing deps
```

**Before merging:** the gate is `.\scripts\pre-merge.ps1` (PowerShell), enforced
automatically by a `pre-push` git hook (`.githooks/pre-push`) — run `make hooks`
once per clone to install it (`make setup` already calls `make hooks`). GitHub
Actions is currently billing-locked on this account (every workflow run fails
to start), so this local gate is the *official* pre-merge check, not a
convenience script. It mirrors `.github/workflows/ci.yml`'s pytest+coverage step
exactly (fails the gate on any test failure or coverage below the 70% floor in
`pyproject.toml`) and also runs ruff+mypy (reported, non-blocking — matches
CI's own `continue-on-error: true` lint job). Prints a single `GATE OK <sha>
tests=<n> coverage=<pct>% ruff=<clean|N> mypy=<clean|N> dirty=<yes|no>` line on
success, or `GATE FAIL <step>` with a non-zero exit otherwise. Pass `-IncludePg`
to also run the real PostgreSQL suite (`scripts/test-postgres.sh`, mirrors the
nightly CI job) — skipped by default since it is slow. Every PR must paste the
literal `GATE OK ...` line (see `.github/pull_request_template.md`) with a
matching `<sha>` and `dirty=no`. Escape hatches: `SKIP_GATE=1 git push` to skip
the gate for one push, or `git push --no-verify` to bypass all hooks —
emergencies only, and say why in the PR.

**Dependencies — single source of truth:** declare/version ALL dependencies in
`pyproject.toml` only (`[project.dependencies]` for runtime, `[dependency-groups].dev`
for tooling). `uv.lock`, `requirements.txt`, and `requirements-dev.txt` are GENERATED
artifacts — never hand-edit them. After changing a dependency, run `make lock` (regenerates
the lockfile and re-exports both requirements files, which are what Docker/Railway install).
`make setup` uses `uv sync` off the lock. This prevents the old drift where requirements.txt
pinned different versions than the working venv.

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
Full procedure, checks and rollback: [`docs/deploy-runbook.md`](docs/deploy-runbook.md).

- **The backend is deployed MANUALLY.** Service `cashin-api` (Railway project `cashing_backend`, environment
  `production`) ships with `railway up --service cashin-api --detach -m "<message>"`, run from
  `cashing-backend-master`. There is NO auto-deploy from GitHub and no deploy job (GitHub Actions is billing-locked),
  so merging to `master` deploys nothing. The frontend on Vercel does auto-deploy from `master`.
- **Boot sequence:** `app.main.lifespan` runs `Base.metadata.create_all` first, then `alembic upgrade head` (or
  `stamp head` when `alembic_version` is empty) as a subprocess. An alembic failure is only a `warning`
  (`alembic_failed`), never a crash, so a deploy can look green while migrations did not run. After EVERY deploy
  confirm the log line `alembic_ok` and check the data/`alembic_version` (`scripts/check_alembic_state.py`).
- **Migrations MUST be idempotent**, because `create_all` has already built the current models when they run. Never
  call `op.create_table` / `op.create_index` / `op.add_column` / `op.create_unique_constraint` /
  `op.create_foreign_key` directly in a new migration; use the helpers in `app/core/migration_helpers.py`
  (`create_table_if_missing`, `create_index_if_missing`, `add_column_if_missing`,
  `create_unique_constraint_if_missing`, `create_foreign_key_if_missing`). The two constraint helpers are
  **PostgreSQL-only**: SQLite cannot ALTER constraints, so on a live SQLite connection they skip when the constraint
  already exists and raise `NotImplementedError` when it does not (use `op.batch_alter_table` marked
  `# migration-guard: ignore <reason>` there). `tests/test_migration_convention_guard.py`
  fails the suite otherwise. Only legacy migrations numbered <= 043 are exempt: every OTHER file under
  `alembic/versions/` is scanned, including hash-named ones. **`make migration` (autogenerate) emits bare
  `op.create_table` / `op.add_column` calls and names files `<hash>_<slug>.py`: edit its output to use the helpers
  before merging.** (Migration `042` did a bare `create_table`, hit `DuplicateTable` in production, and left `043`
  unapplied.)
  - The guard is **not exhaustive**: `op.create_check_constraint`, `op.create_primary_key`, PostgreSQL enum creation
    and DDL built in variables or run through `bind.execute` are not detected and have no helper. Make those
    idempotent by hand (existence check / `DO` block) or mark the call `# migration-guard: ignore <reason>` (the
    reason is mandatory). Constraint helpers match by NAME only.
  - Other known blind spots of the guard: a parameter/local variable named `op` is flagged (use the marker); raw
    DDL text inside a data statement (`op.execute("UPDATE ... SET note='CREATE TABLE x'")`) is flagged (use the
    marker); a batch context re-bound through another name (`c = b`) is not resolved; DDL built in variables or via
    `bind.execute` is not seen. The marker is read only from real comments and covers a whole physical line, so keep
    one call per line. Inside a batch context there is no idempotent helper: guard by hand or use the marker.
  - Offline SQL (`alembic upgrade --sql`) works, but cannot be idempotent: the helpers emit the plain DDL there.
- **`create_all` shape is permanent for tables it built.** Production's `paquete_job` (and any table `create_all`
  created before its migration ran) has the `create_all` shape, not the migration's: e.g. `status` has a Python-side
  default only, while migration `042` declares `server_default="pending"`. The idempotent helpers only check
  existence: they do not repair shape divergence and do not add a missing unique constraint or server default to an
  existing table. Fixing that is a dedicated migration.
- **No redeploy rollback:** older deployments become `REMOVED`, so there is nothing to redeploy. To roll back, build a
  worktree at the previous commit and `railway up` from it.
- **Secrets:** NEVER run `railway variables --kv` (it dumps every secret into the terminal/logs); list variable
  names only.
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
