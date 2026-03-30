# CashIn Backend — Stack de Desarrollo (TOOLS)

> Define herramientas de desarrollo local, QA, emulación y deployment.
> MVP desplegado en **Railway** — sin dependencia de AWS, GCP o Azure.
> Adaptadores cloud listos para integración futura si se migra.

---

## 1. Gestión de Entorno y Dependencias

### Gestor: `uv` (obligatorio)

```bash
# Instalar uv (Windows PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Setup completo
uv venv
uv pip install -r requirements.txt
uv pip install -r requirements-dev.txt
```

### Makefile (todos los comandos)

| Target                     | Comando                                      | Propósito                  |
| -------------------------- | -------------------------------------------- | -------------------------- |
| `make setup`               | `uv venv + pip install + pre-commit install` | Setup inicial              |
| `make dev`                 | `uvicorn --reload` en :8000                  | Servidor desarrollo        |
| `make run`                 | `uvicorn` sin reload                         | Servidor producción local  |
| `make up`                  | `docker compose up -d`                       | PostgreSQL + MinIO + Redis |
| `make down`                | `docker compose down`                        | Parar servicios Docker     |
| `make migrate`             | `alembic upgrade head`                       | Aplicar migraciones        |
| `make migration msg="..."` | `alembic revision --autogenerate`            | Generar migración          |
| `make test`                | `pytest -v --tb=short`                       | Correr tests               |
| `make test-cov`            | `pytest --cov=app --cov-report=html`         | Coverage (70% min)         |
| `make lint`                | `ruff check + format --check + mypy`         | Lint + tipos               |
| `make format`              | `ruff check --fix + ruff format`             | Auto-fix estilo            |
| `make security`            | `bandit + pip-audit`                         | Scan vulnerabilidades      |
| `make clean`               | Eliminar `__pycache__`, `.pyc`, caches       | Limpiar proyecto           |

---

## 2. Tooling de Calidad

### Linters y Tipos (configurados en pyproject.toml)

```bash
ruff check .          # Lint (E, W, F, I, N, UP, B, S, T20, SIM, RUF)
ruff format .         # Formatter (120 chars)
mypy app/             # Type check strict + Pydantic plugin
```

### Paquetes QA (en requirements-dev.txt)

- `pytest` + `pytest-asyncio` — Tests async automáticos
- `httpx` — AsyncClient para tests HTTP sin servidor
- `moto[s3]` — Mock de S3-compatible storage
- `factory-boy` — Factories para generación de datos
- `coverage[toml]` — Cobertura con config en pyproject.toml
- `ruff` — Linter/formatter ultra-rápido
- `mypy` — Type checker estricto
- `bandit` — Análisis de seguridad del código
- `pip-audit` — Análisis de vulnerabilidades en dependencias
- `pre-commit` — Hooks pre-commit (secrets, large files, ruff)

---

## 3. Extensiones VS Code

### Obligatorias

- `ms-python.python` — Python runtime
- `ms-python.vscode-pylance` — Análisis estático + IntelliSense
- `charliermarsh.ruff` — Ruff linter/formatter integrado
- `tamasfe.even-better-toml` — Soporte pyproject.toml
- `redhat.vscode-yaml` — YAML/Docker Compose

### Recomendadas

- `ms-azuretools.vscode-docker` — Docker management
- `ms-vscode.makefile-tools` — Makefile integration
- `github.copilot` + `github.copilot-chat` — AI assistant
- `eamodio.gitlens` — Git history

### Evitar

- No tener múltiples formatters activos. Usar solo Ruff.

---

## 4. Emulación Local (Docker Compose)

### Servicios Locales (docker-compose.yml)

```yaml
services:
  db: # PostgreSQL 16-alpine → localhost:5432
  minio: # MinIO (S3-compatible) → localhost:9000 (API), :9001 (console)
  redis: # Redis 7-alpine → localhost:6379
  app: # FastAPI (Dockerfile.dev) → localhost:8000
```

**Credenciales locales por defecto:**

- PostgreSQL: `cashin:cashin_local@localhost:5432/cashin`
- MinIO: `minioadmin:minioadmin@localhost:9000`
- Redis: `localhost:6379` (sin auth)

### Flujo Operativo Local

```bash
make up          # 1. Levantar PostgreSQL + MinIO + Redis
make migrate     # 2. Aplicar migraciones Alembic
make dev         # 3. Servidor con hot-reload en :8000
# Navegar a http://localhost:8000/docs para OpenAPI
```

### Variables de Entorno (desarrollo local, .env)

```env
ENVIRONMENT=development
DATABASE_URL=postgresql+asyncpg://cashin:cashin_local@localhost:5432/cashin
JWT_SECRET_KEY=dev-secret-key-min-32-characters-long
TOKEN_ENCRYPTION_KEY=<fernet-key-from-generate_secrets.py>
S3_ENDPOINT_URL=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_REGION=auto
S3_BUCKET_EVIDENCIAS=cashin-evidencias
S3_BUCKET_DOCUMENTOS=cashin-documentos
S3_BUCKET_PDFS=cashin-pdfs
LLM_DEFAULT_MODEL=gemini/gemini-2.0-flash-lite
LLM_FALLBACK_MODEL=openai/gpt-4o-mini
LLM_LOCAL_MODEL=ollama/llama3.1
GEMINI_API_KEY=<tu-key>
OPENAI_API_KEY=<tu-key>
CORS_ORIGINS=["http://localhost:19006","http://localhost:3000"]
```

---

## 5. Testing

### Stack de Tests

- `pytest-asyncio` con `asyncio_mode = "auto"` — sin `@pytest.mark.asyncio`
- `httpx.AsyncClient` con ASGI transport — sin levantar servidor HTTP
- SQLite in-memory (`aiosqlite`) — DB rápida y aislada por test
- `moto[s3]` — Mock de operaciones S3 sin servicios reales
- `factory-boy` — Factories para datos de test
- Rate limiter deshabilitado globalmente en tests

### Estructura

```
tests/
├── conftest.py          # Fixtures: db, client, test_user
├── test_health.py       # Health endpoint (2 tests)
├── test_auth_service.py # Auth business logic (9+ tests)
├── test_auth_api.py     # Auth HTTP endpoints (18+ tests)
└── test_agent.py        # Agent, LLM, tools (20+ tests)
```

### Comandos

```bash
make test                                             # Todo
make test-cov                                         # Con coverage HTML
uv run pytest tests/test_auth_api.py -v               # Archivo
uv run pytest tests/test_auth_api.py::test_login -v   # Función
```

---

## 6. Deploy MVP — Railway

### Configuración Actual

**railway.toml:**

```toml
[deploy]
startCommand = "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2"
healthcheckPath = "/health"
healthcheckTimeout = 10
restartPolicyType = "on_failure"
restartPolicyMaxRetries = 3

[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"
```

**Dockerfile (producción):**

- Base: `python:3.12-slim`
- Instala: libmagic, Cairo (WeasyPrint), crea usuario non-root `cashin`
- Entrypoint: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2`

**Startup sequence:**

1. Build imagen Docker
2. `alembic upgrade head` (migraciones automáticas)
3. `uvicorn` con 2 workers
4. Health check: GET `/health` con 10s timeout
5. Restart on failure (max 3 reintentos)

### Variables de Entorno en Railway

```
DATABASE_URL=postgresql+asyncpg://<railway-provides>
JWT_SECRET_KEY=<generated-secret-min-32-chars>
TOKEN_ENCRYPTION_KEY=<fernet-key>
ENVIRONMENT=production
GEMINI_API_KEY=<key>
S3_ENDPOINT_URL=<cloudflare-r2-endpoint>
S3_ACCESS_KEY=<r2-access-key>
S3_SECRET_KEY=<r2-secret-key>
```

### Por qué Railway para MVP

- Deploy desde GitHub push (zero-config CI/CD)
- PostgreSQL managed incluido
- $5-10/mes para MVP
- Sin vendor lock-in de cloud providers (AWS/GCP/Azure)
- Migración trivial: la app es un contenedor Docker estándar

---

## 7. Preparación para Cloud (Futuro, No MVP)

> **IMPORTANTE:** Estas configuraciones NO se implementan en el MVP.
> Quedan documentadas como referencia para migración futura.
> Todo el MVP corre en Railway + Cloudflare R2 + PostgreSQL managed.

### 7.1 AWS (si se migra)

**Entry point adicional:**

```python
# handler.py (solo si se migra a Lambda)
from mangum import Mangum
from app.main import app
handler = Mangum(app, lifespan="off")
```

**Servicios equivalentes:**
| CashIn Actual | AWS Equivalente |
|---|---|
| Railway (container) | ECS Fargate / Lambda + API Gateway |
| PostgreSQL managed | RDS PostgreSQL |
| Cloudflare R2 | S3 |
| Redis local | ElastiCache |
| JWT custom | Cognito (opcional) |

**Adapter necesario:** `adapters/aws/` (no implementado aún)

### 7.2 GCP (si se migra)

**Deploy:**

```bash
# Cloud Run (contenedor Docker, zero changes)
gcloud run deploy cashin-backend --source .
```

**Servicios equivalentes:**
| CashIn Actual | GCP Equivalente |
|---|---|
| Railway (container) | Cloud Run |
| PostgreSQL managed | Cloud SQL PostgreSQL |
| Cloudflare R2 | Cloud Storage |
| Redis local | Memorystore |
| JWT custom | Firebase Auth (opcional) |

**Adapter necesario:** `adapters/gcp/` (no implementado aún)

### 7.3 Azure (si se migra)

**Servicios equivalentes:**
| CashIn Actual | Azure Equivalente |
|---|---|
| Railway (container) | Azure Container Apps |
| PostgreSQL managed | Azure Database for PostgreSQL |
| Cloudflare R2 | Azure Blob Storage |
| Redis local | Azure Cache for Redis |
| JWT custom | Azure AD B2C (opcional) |

**Adapter necesario:** `adapters/azure/` (no implementado aún)

### 7.4 Principio de Portabilidad

La app está diseñada con **Ports & Adapters** para que migrar sea escribir un nuevo adapter, no refactorizar el core:

```
app/adapters/
├── llm/
│   ├── port.py              # Protocol (interfaz)
│   └── litellm_adapter.py   # ✅ Implementado
├── storage/
│   ├── port.py              # Protocol (interfaz)
│   └── s3_adapter.py        # ✅ Implementado (funciona con MinIO, R2, S3, GCS)
├── aws/                     # ⬚ Futuro: Cognito, SQS, SES adapters
├── gcp/                     # ⬚ Futuro: Firebase, Pub/Sub adapters
└── azure/                   # ⬚ Futuro: AD B2C, Service Bus adapters
```

**Regla:** El core de negocio (`services/`, `agent/`, `models/`) **nunca** importa SDKs de cloud providers. Solo usa `Port` protocols.

---

## 8. LLM — Configuración y Providers

### Abstracción via LiteLLM

```python
# Cambiar modelo sin tocar código:
LLM_DEFAULT_MODEL=gemini/gemini-2.0-flash-lite   # Primario (~$0.075/1M tokens)
LLM_FALLBACK_MODEL=openai/gpt-4o-mini             # Fallback (~$0.15/1M tokens)
LLM_LOCAL_MODEL=ollama/llama3.1                    # Local ($0, para dev/test)
```

### Fallback Chain

1. Intenta modelo primario (Gemini)
2. Si falla → modelo fallback (OpenAI)
3. Si falla → modelo local (Ollama)
4. Si todo falla → `RuntimeError`

### Retry: 2 reintentos con backoff exponencial (1-4s)

---

## 9. Google Workspace (Fase 4, No MVP)

> Preparado pero NO implementado en MVP.

### Librerías (ya en requirements.txt)

- `google-api-python-client`
- `google-auth`, `google-auth-oauthlib`, `google-auth-httplib2`

### Variables de Entorno (setear cuando se implemente)

```env
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/integraciones/google/callback
```

### Scopes mínimos (readonly)

- `gmail.readonly` — Lectura de correos por rango de fechas
- `calendar.readonly` — Eventos por rango temporal
- `drive.metadata.readonly` — Metadata de archivos

---

## 10. Scripts Utilitarios

```bash
python scripts/generate_secrets.py  # Genera JWT_SECRET_KEY + TOKEN_ENCRYPTION_KEY
python scripts/load_secrets.py      # Carga secrets/.env.local en .env
```

### generate_secrets.py

- Genera secret JWT de 256 bits (hex)
- Genera Fernet key para encriptar tokens OAuth
- Escribe a `secrets/.env.local`

### setup_gcloud.ps1 (solo si migra a GCP)

- Crea proyecto GCP + habilita APIs
- Configura OAuth consent screen
