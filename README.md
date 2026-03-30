# CashIn Backend

AI Agent-first backend for automating Colombian contractor billing (cuentas de cobro).

**Cloud-agnostic** — MVP deployed on **Railway**. No AWS/GCP/Azure dependency.
Ports & Adapters architecture enables future cloud migration without touching core business logic.

## Stack

| Layer     | Technology            | Purpose                                         |
| --------- | --------------------- | ----------------------------------------------- |
| Runtime   | Python 3.12 + FastAPI | Async API framework                             |
| AI Engine | LangGraph + LiteLLM   | Agent workflows + LLM abstraction (100+ models) |
| Database  | PostgreSQL 16         | Primary data (Railway managed in prod)          |
| Storage   | Cloudflare R2 / MinIO | S3-compatible object storage                    |
| Payments  | Wompi                 | Colombian payment gateway                       |
| Deploy    | Railway               | Docker container (zero-config CI/CD)            |

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

## Architecture

```
HTTP Request → FastAPI (api/v1/) → Service Layer → LangGraph Agent → LLM + DB + Storage
```

### Ports & Adapters (cloud-agnostic)

```
app/adapters/
├── llm/
│   ├── port.py              # LLMPort Protocol
│   └── litellm_adapter.py   # Gemini → OpenAI → Ollama (fallback chain)
├── storage/
│   ├── port.py              # StoragePort Protocol
│   └── s3_adapter.py        # MinIO (dev) / R2 (prod) / any S3-compatible
```

The core (`services/`, `agent/`, `models/`) **never** imports cloud SDKs directly.

## Project Structure

```
app/
├── api/        # FastAPI routes (/api/v1/)
├── agent/      # LangGraph AI agent (nodes, prompts, tools)
├── models/     # SQLAlchemy models (16 domain models)
├── schemas/    # Pydantic v2 schemas
├── services/   # Business logic layer
├── adapters/   # Ports & Adapters (storage, LLM)
├── core/       # Config, DB, security, exceptions, middleware
└── main.py     # FastAPI app entry point
```

## Environment

Copy `.env.example` → `.env` and configure:

- `DATABASE_URL` — PostgreSQL connection string
- `JWT_SECRET_KEY` — Min 32 chars (use `scripts/generate_secrets.py`)
- `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` — Storage
- `GEMINI_API_KEY` / `OPENAI_API_KEY` — LLM providers
- `WOMPI_*` keys — Payment gateway (optional for dev)

## Deploy (Railway)

Push to GitHub → Railway auto-deploys via `Dockerfile`.

- Start: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2`
- Health check: `GET /health`
- PostgreSQL managed by Railway

## License

Proprietary — All rights reserved.
