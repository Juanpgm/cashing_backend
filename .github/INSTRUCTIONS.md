# CashIn Backend — Instrucciones Canónicas

> Fuente de verdad para arquitectura, reglas de código, roadmap y flujo operativo.
> Claude Code y GitHub Copilot deben priorizar este documento sobre sugerencias genéricas.

---

## 1. Visión del Producto

**CashIn** es un backend AI Agent-first que automatiza la creación de cuentas de cobro para contratistas colombianos de prestación de servicios. Un agente IA (LangGraph) procesa contratos, extrae obligaciones, clasifica actividades, recolecta evidencia, genera justificaciones y ensambla documentos finales (PDF).

**Usuario objetivo:** Contratista colombiano que necesita generar su cuenta de cobro mensual con evidencias organizadas.

**Flujo de valor:**

```
Contrato (PDF/DOCX) → Agente IA → Extracción de obligaciones
                                  → Clasificación de actividades (LABORAL/NO_LABORAL/PARCIAL)
                                  → Generación de justificación formal en español colombiano
                                  → Ensamble de PDF final con carpetas de evidencia
```

---

## 2. Arquitectura

### 2.1 Stack Tecnológico (Implementado)

| Capa                | Tecnología                        | Versión                | Propósito                               |
| ------------------- | --------------------------------- | ---------------------- | --------------------------------------- |
| Runtime             | Python                            | 3.12+                  | Lenguaje core                           |
| Framework           | FastAPI                           | 0.115+                 | API async con OpenAPI auto              |
| ORM                 | SQLAlchemy 2.0 async              | asyncpg driver         | Modelos + queries                       |
| DB                  | PostgreSQL                        | 16                     | Base de datos relacional                |
| Agente IA           | LangGraph                         | 0.4+                   | Grafos de workflow con state management |
| LLM                 | LiteLLM                           | 1.60+                  | 100+ modelos, fallback chains           |
| Storage             | S3-compatible                     | boto3                  | MinIO (dev), Cloudflare R2 (prod)       |
| Auth                | python-jose + bcrypt              | JWT HS256              | Tokens access/refresh                   |
| Pagos               | Wompi                             | httpx                  | Pasarela colombiana                     |
| PDF                 | WeasyPrint + Jinja2               | —                      | HTML → PDF rendering                    |
| Parsing             | pdfplumber, python-docx, openpyxl | —                      | Extracción de texto                     |
| Logging             | structlog                         | JSON prod, consola dev | Logging estructurado                    |
| Rate Limit          | slowapi                           | —                      | Throttling por IP                       |
| Validación archivos | python-magic                      | —                      | MIME type detection                     |

### 2.2 Capas de la Aplicación

```
┌─────────────────────────────────────────────────┐
│  API Layer (app/api/)                           │
│  FastAPI routes, deps, rate limits, auth guards │
│  Archivos: deps.py, router.py, v1/*.py          │
├─────────────────────────────────────────────────┤
│  Service Layer (app/services/)                  │
│  Orquestación, lógica de negocio, persistencia  │
│  Archivos: agent_service, auth_service,         │
│            document_service                      │
├─────────────────────────────────────────────────┤
│  Agent Layer (app/agent/)                       │
│  LangGraph grafo, nodos, prompts, tools         │
│  Archivos: graph.py, state.py, nodes/, prompts/,│
│            tools/                                │
├─────────────────────────────────────────────────┤
│  Adapter Layer (app/adapters/)                  │
│  Ports & Adapters: LLM, Storage                 │
│  Archivos: llm/port.py + litellm_adapter.py,   │
│            storage/port.py + s3_adapter.py       │
├─────────────────────────────────────────────────┤
│  Core Layer (app/core/)                         │
│  Config, DB, Security, Exceptions, Middleware    │
│  Archivos: config.py, database.py, security.py, │
│    exceptions.py, audit.py, rate_limit.py,       │
│    file_validation.py, security_headers.py       │
├─────────────────────────────────────────────────┤
│  Model Layer (app/models/)                      │
│  SQLAlchemy ORM, mixins, 16 domain entities      │
├─────────────────────────────────────────────────┤
│  Schema Layer (app/schemas/)                    │
│  Pydantic v2 request/response validation         │
│  Archivos: agent.py, auth.py, common.py          │
└─────────────────────────────────────────────────┘
```

### 2.3 Flujo de Request

```
HTTP Request
  → SecurityHeadersMiddleware (CSP, X-Frame-Options, HSTS)
  → AuditMiddleware (trace_id, logging)
  → CORSMiddleware (origin whitelist)
  → slowapi Rate Limiter (por IP)
  → FastAPI Router
  → Dependency Injection (get_db, get_current_user, require_credits)
  → Service Layer
  → LangGraph Agent / DB / Storage
  → Response (JSON o SSE stream)
```

### 2.4 Flujo del Agente IA (LangGraph)

```
[User Input] → [Router Node] (LLM clasifica intención)
  ├── mode=CHAT     → [Chat Node] → respuesta conversacional → END
  ├── mode=PIPELINE → [Doc Ingestion] → [Doc Understanding] → [Classification] → [Justification] → END
  └── mode=CONFIG   → [Config Handler] → END
```

**Temperaturas por nodo:**

- Router: 0.0 (determinista, una palabra)
- Chat: 0.4 (conversacional balanceado)
- Understanding: 0.1 (extracción precisa)
- Classification: 0.0 (estricta)
- Justification: 0.3 (creativa pero consistente)

### 2.5 Ports & Adapters

| Puerto (Protocol)                                                       | Adaptador Implementado | Alternativas Cloud                       |
| ----------------------------------------------------------------------- | ---------------------- | ---------------------------------------- |
| `LLMPort` (`complete()`, `stream()`)                                    | `LiteLLMAdapter`       | Gemini Flash-Lite → GPT-4o-mini → Ollama |
| `StoragePort` (`upload()`, `download()`, `presigned_url()`, `delete()`) | `S3Adapter`            | MinIO (dev), R2 (prod), AWS S3           |

**Regla:** Nunca importar `boto3` directamente en services. Siempre usar `StoragePort`.

---

## 3. Reglas de Código

### 3.1 Estilo y Formato

- **Line length:** 120 caracteres
- **Formatter/Linter:** Ruff (rules: E, W, F, I, N, UP, B, S, T20, SIM, RUF)
- **Type checker:** mypy strict con plugin Pydantic
- **Imports:** Ordenados por isort (vía Ruff `I`)
- **Logging:** `structlog.get_logger("modulo.submodulo")` — nunca `print()`

### 3.2 Patrones Obligatorios

```python
# ✅ Async everywhere
async def create_user(db: AsyncSession, data: RegisterRequest) -> Usuario:

# ✅ Pydantic v2 para validación
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

# ✅ Domain exceptions (nunca HTTPException directo en services)
raise NotFoundError("Contrato", contrato_id)

# ✅ Dependency injection via FastAPI
@router.post("/")
async def create(data: Schema, db: AsyncSession = Depends(get_db), user: Usuario = Depends(get_current_user)):

# ✅ TypedDict para state del agente (total=False)
class AgentState(TypedDict, total=False):
    messages: list[LLMMessage]
    mode: AgentMode

# ✅ Nodos retornan spread parcial
def node(state: AgentState) -> dict:
    return {**state, "key": new_value}
```

### 3.3 Anti-Patrones (Prohibidos)

```python
# ❌ Raw SQL → usar SQLAlchemy ORM
# ❌ HTTPException en service layer → usar domain exceptions
# ❌ Sync database calls → siempre AsyncSession
# ❌ Acoplar a cloud directo → usar StoragePort/LLMPort
# ❌ print() → usar structlog
# ❌ Hardcoded secrets → usar Settings via .env
# ❌ shell=True en subprocess → nunca
# ❌ float para montos → usar Decimal
```

### 3.4 Nomenclatura

| Elemento        | Convención            | Ejemplo                             |
| --------------- | --------------------- | ----------------------------------- |
| Archivos Python | snake_case            | `agent_service.py`                  |
| Clases          | PascalCase            | `CuentaCobro`, `StoragePort`        |
| Funciones       | snake_case            | `create_cuenta_cobro()`             |
| Constantes      | UPPER_SNAKE           | `CREDITS_PER_CHAT_MESSAGE`          |
| Modelos DB      | Singular español      | `Usuario`, `Contrato`, `Obligacion` |
| Schemas         | PascalCase + Sufijo   | `LoginRequest`, `TokenResponse`     |
| Tests           | `test_` + descripción | `test_login_wrong_password`         |
| Endpoints       | snake_case en path    | `/api/v1/cuentas_cobro`             |

### 3.5 Reglas de Desacoplamiento

1. `Router → Service → DB`. Nunca Router → DB directo.
2. Services no importan `Request`, `HTTPException`. Usan excepciones de dominio.
3. Modelos ORM no se retornan en endpoints. Siempre mapear a schemas.
4. Integraciones externas encapsuladas en adapters para mocking.
5. Todo servicio testeable sin levantar servidor HTTP.

---

## 4. Modelos de Datos (16 Entidades)

### Mixins Base (app/models/base.py)

- `UUIDMixin` — PK UUID4
- `TimestampMixin` — `created_at`, `updated_at` con server defaults
- `SoftDeleteMixin` — Borrado lógico via `deleted_at`

### Entidades Core

| Modelo        | Tabla         | Descripción                                                            |
| ------------- | ------------- | ---------------------------------------------------------------------- |
| `Usuario`     | usuarios      | Contratista (email, password_hash, créditos, rol, brute-force counter) |
| `Contrato`    | contratos     | Contrato de prestación (número, objeto, valor, supervisor)             |
| `Obligacion`  | obligaciones  | Obligaciones contractuales (general/específica, orden)                 |
| `CuentaCobro` | cuentas_cobro | Cuenta mensual (estado: borrador→enviada→aprobada→pagada)              |
| `Actividad`   | actividades   | Actividades realizadas por obligación                                  |
| `Evidencia`   | evidencias    | Archivos soporte (storage_key S3, tipo, tamaño)                        |

### Entidades Soporte

| Modelo            | Tabla             | Descripción                                     |
| ----------------- | ----------------- | ----------------------------------------------- |
| `DocumentoFuente` | documentos_fuente | Docs subidos (contrato/instrucciones/plantilla) |
| `Plantilla`       | plantillas        | Templates HTML/Jinja2 para PDF                  |
| `Conversacion`    | conversaciones    | Historial de chat (mensajes_json)               |

### Entidades Monetización

| Modelo        | Tabla         | Descripción                               |
| ------------- | ------------- | ----------------------------------------- |
| `Credito`     | creditos      | Ledger de créditos (compra/consumo/bonus) |
| `Pago`        | pagos         | Registros de pago Wompi                   |
| `Suscripcion` | suscripciones | Planes (free/basico/pro)                  |

### Entidades Auth & Audit

| Modelo           | Tabla           | Descripción                       |
| ---------------- | --------------- | --------------------------------- |
| `TokenBlacklist` | token_blacklist | JTIs revocados                    |
| `GoogleToken`    | google_tokens   | OAuth tokens encriptados (Fernet) |
| `AuditLog`       | audit_logs      | Trail de auditoría                |

---

## 5. Endpoints API (Implementados)

### Auth (`/api/v1/auth`)

| Método | Path        | Rate Limit | Auth   | Descripción                      |
| ------ | ----------- | ---------- | ------ | -------------------------------- |
| POST   | `/register` | 5/min      | No     | Registro con 30 créditos gratis  |
| POST   | `/login`    | 5/min      | No     | Login → access + refresh tokens  |
| POST   | `/refresh`  | 10/min     | No     | Renovar tokens (old blacklisted) |
| GET    | `/me`       | —          | Bearer | Perfil del usuario actual        |
| PUT    | `/me`       | —          | Bearer | Actualizar perfil                |
| POST   | `/logout`   | —          | Bearer | Invalidar token actual           |

### Chat (`/api/v1/chat`)

| Método | Path            | Rate Limit | Auth   | Descripción               |
| ------ | --------------- | ---------- | ------ | ------------------------- |
| POST   | `/`             | 20/min     | Bearer | Enviar mensaje al agente  |
| POST   | `/stream`       | 20/min     | Bearer | SSE stream de respuesta   |
| GET    | `/{session_id}` | —          | Bearer | Historial de conversación |

### Documentos (`/api/v1/documentos`)

| Método | Path       | Rate Limit | Auth   | Descripción                  |
| ------ | ---------- | ---------- | ------ | ---------------------------- |
| POST   | `/upload`  | 10/min     | Bearer | Subir contrato/instrucciones |
| POST   | `/process` | 10/min     | Bearer | Reprocesar documento         |

### Health

| GET | `/health` | — | No | Estado del servicio + environment |

---

## 6. Seguridad (Implementada)

### Autenticación

- Bcrypt cost 12 para passwords
- JWT HS256: access 15min, refresh 7 días
- Token blacklist en PostgreSQL (JTI)
- Brute force: lock después de 10 intentos fallidos
- Refresh tokens de un solo uso (old → blacklist)

### Protección API

- Rate limiting: 100/min global, 5/min auth, 20/min chat, 10/min upload
- CORS whitelist configurable
- Security headers: CSP, `X-Frame-Options: DENY`, `X-XSS-Protection`, HSTS (prod)
- Audit trail con trace_id UUID por request

### Archivos

- Validación MIME con magic bytes + extension whitelist
- Extensiones permitidas: pdf, jpg, jpeg, png, docx, xlsx, pptx, xls
- Prevención path traversal, no doble extensiones
- Límite 10MB por archivo

### LLM

- Separación system/user messages contra prompt injection
- Data minimization en contexto enviado al LLM
- Input truncado a 8000 chars para extraction

---

## 7. Roadmap

| Fase | Nombre                                                   | Estado         |
| ---- | -------------------------------------------------------- | -------------- |
| 1    | Cimientos (DB, Auth, Core, Storage)                      | ✅ Completada  |
| 2    | Motor del Agente IA (LangGraph, LLM, Tools)              | ✅ Completada  |
| 3    | Contratos, Cuentas de Cobro, Plantillas (CRUD + estados) | 🔄 En progreso |
| 4    | Integración Google Workspace + Evidencias                | ⬚ Pendiente    |
| 5    | Pagos y Monetización (Wompi + créditos)                  | ⬚ Pendiente    |
| 6    | Hardening de Seguridad (transversal)                     | 🔄 Continuo    |
| 7    | GCP CLI Setup + OAuth Configuration                      | ⬚ Pendiente    |
| 8    | Production-Ready (optimización, caché, notificaciones)   | ⬚ Pendiente    |

---

## 8. Testing

### Stack

- `pytest-asyncio` con `asyncio_mode = "auto"` (no marcar tests async)
- DB test: SQLite in-memory (`aiosqlite`)
- Storage mock: `moto[s3]`
- Rate limiter: deshabilitado globalmente en tests
- Coverage mínimo: 70%

### Tests Existentes

```
tests/conftest.py          # Fixtures: db, client, test_user
tests/test_health.py       # GET /health, GET /docs
tests/test_auth_service.py # Business logic auth (9+ tests)
tests/test_auth_api.py     # HTTP auth endpoints (18+ tests)
tests/test_agent.py        # Agent, LLM, tools (20+ tests)
```

### Comandos

```bash
make test                                           # pytest -v --tb=short
make test-cov                                       # con HTML coverage
uv run pytest tests/test_auth_api.py::test_login -v # individual
```

---

## 9. Deploy

### Local

```bash
make setup    # uv venv + deps + pre-commit
make up       # Docker: PostgreSQL 16 + MinIO + Redis
make migrate  # alembic upgrade head
make dev      # uvicorn --reload en :8000
```

### Producción (Railway)

```toml
# railway.toml
startCommand = "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 2"
healthcheckPath = "/health"
```

### Variables Críticas

- `DATABASE_URL` — `postgresql+asyncpg://...`
- `JWT_SECRET_KEY` — Min 32 chars
- `TOKEN_ENCRYPTION_KEY` — Fernet key
- `GEMINI_API_KEY` / `OPENAI_API_KEY`
- `S3_ENDPOINT_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`
- `WOMPI_PUBLIC_KEY`, `WOMPI_PRIVATE_KEY`, `WOMPI_EVENTS_SECRET`

---

## 10. Flujo Operativo

### Antes de cada tarea

1. Leer fase correspondiente del roadmap
2. Branch: `feat/fase-X-descripcion` o `fix/descripcion`
3. Implementar por capas: model → schema → service → api → test

### Antes de commit

```bash
make format    # Auto-fix estilo con Ruff
make lint      # Verificar ruff + mypy strict
make test      # Correr pytest
make security  # bandit + pip-audit
```

### Para nueva migración

```bash
make migration msg="descripcion del cambio"
make migrate
```

### Convención de commits

- `feat(modulo): descripcion`
- `fix(modulo): descripcion`
- `test(modulo): descripcion`
- `docs: descripcion`
- `refactor(modulo): descripcion`

---

## 11. Sistema de Créditos y Pagos

### Costos por Acción

| Acción                   | Créditos   |
| ------------------------ | ---------- |
| Crear cuenta de cobro    | 10         |
| Mensaje de chat          | 1          |
| Recolección de evidencia | 5          |
| Registro (bonus)         | +30 gratis |

### Planes de Suscripción

| Plan   | Créditos/mes | Precio |
| ------ | ------------ | ------ |
| Free   | 0            | $0     |
| Básico | 100          | TBD    |
| Pro    | 500          | TBD    |

### Integración Wompi

- Webhook con verificación HMAC
- Estados: pendiente → aprobado/rechazado/error
- Idempotencia por `referencia_wompi`

---

## 12. Filosofía Cloud-Agnostic

### Principio Fundamental

> **El MVP corre en Railway.** No hay dependencia de AWS, GCP ni Azure.
> La arquitectura Ports & Adapters permite escribir un nuevo adapter sin tocar el core.

### Reglas de Portabilidad

- FastAPI como app única, contenedor Docker estándar
- Puertos/interfaces para servicios externos: `StoragePort`, `LLMPort`
- Configuración 100% por environment variables (`pydantic-settings`)
- UTC siempre: `datetime.now(timezone.utc)`
- El core (`services/`, `agent/`, `models/`) **nunca** importa SDKs de cloud

### Stack MVP (actual)

| Servicio | MVP (Railway)                  | Dev local         |
| -------- | ------------------------------ | ----------------- |
| Auth     | JWT custom (python-jose HS256) | Igual             |
| Storage  | Cloudflare R2 (S3-compatible)  | MinIO             |
| LLM      | Gemini → OpenAI (via LiteLLM)  | Ollama            |
| DB       | PostgreSQL managed (Railway)   | PostgreSQL Docker |
| Cache    | —                              | Redis Docker      |
| Deploy   | Railway (Docker)               | Docker Compose    |

### Migración Futura (no implementado)

Si se migra a un cloud provider, solo se requiere:

1. Escribir nuevo adapter en `app/adapters/{provider}/`
2. Cambiar variables de entorno
3. Opcional: cambiar entry point (e.g., `Mangum` para Lambda)

Posibles paths:

- **AWS:** ECS/Lambda, RDS, S3, SQS, Cognito
- **GCP:** Cloud Run, Cloud SQL, Cloud Storage, Pub/Sub, Firebase Auth
- **Azure:** Container Apps, Azure DB for PostgreSQL, Blob Storage, Service Bus

### Google Workspace (Fase 4, No MVP)

- OAuth 2.0 Authorization Code + PKCE
- Scopes mínimos: gmail.readonly, calendar.readonly, drive.metadata.readonly
- Tokens refresh cifrados con Fernet
- Aislamiento estricto por usuario
