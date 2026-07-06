# CashIn Backend — Competencias Técnicas (SKILLS)

> Define competencias requeridas, criterios de diseño y patrones de implementación.
> Referencia para Claude Code, Copilot y desarrolladores.

---

## 1. Python 3.12 + FastAPI (Core Stack)

### Competencias

- Asincronía moderna: `async/await` en todo I/O (DB, HTTP, storage)
- Tipado estricto: type hints en todo módulo público, `mypy --strict`
- Tipos avanzados: `Protocol`, `TypedDict`, `Literal`, `Annotated`
- FastAPI: routers versionados, `Depends()`, middlewares, OpenAPI auto
- Pydantic v2: `BaseModel`, `Field`, `field_validator`, `model_validator`, `ConfigDict`

### Patrones Implementados

```python
# Inyección de dependencias
CurrentUser = Annotated[Usuario, Depends(get_current_user)]

# Schemas con validación
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)

# Error handling centralizado
raise NotFoundError("Contrato", contrato_id)  # → 404 automático
```

### Criterio de Calidad

- Zero `print()` — solo structlog
- Zero `float` para montos — solo `Decimal`
- Zero raw SQL — solo SQLAlchemy ORM
- Line length: 120 chars (configurado en Ruff)

---

## 2. SQLAlchemy 2.0 Async + PostgreSQL 16 + Alembic

### Competencias

- `AsyncSession` con asyncpg driver
- Query style 2.0: `select()`, `scalars()`, `execute()`
- Carga eficiente: `selectinload()`, `joinedload()` — evitar N+1
- Mixins: `UUIDMixin`, `TimestampMixin`, `SoftDeleteMixin`
- Transacciones explícitas en services
- Alembic async con autogeneración + revisión manual

### Patrones Implementados

```python
# Session per request (dependency)
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
        await session.commit()

# Query pattern
stmt = select(Usuario).where(Usuario.email == email)
result = await db.execute(stmt)
user = result.scalar_one_or_none()

# Modelo con mixins
class CuentaCobro(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "cuentas_cobro"
    contrato_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contratos.id"))
    estado: Mapped[str] = mapped_column(default="borrador")
```

### Índices Requeridos

- `usuario_id` en todas las tablas de usuario
- `estado` en `cuentas_cobro`
- `(contrato_id, mes, anio)` unique constraint
- `email` unique en `usuarios`
- `jti` unique en `token_blacklist`

---

## 3. LangGraph + LiteLLM + MCP (Motor IA Agéntico)

### Competencias

- Diseño de grafos de workflow con nodos tipados y múltiples modos
- State management con `TypedDict(total=False)` y spread pattern
- Streaming SSE para respuestas en tiempo real
- Fallback chains: primario → fallback → local (Ollama)
- Prompt engineering en español colombiano profesional
- Token management y cost optimization (modelo correcto por tarea)
- MCP server development con `mcp[cli]` (Python)
- Integración de herramientas externas como nodos LangGraph

### AgentState (Estado del Agente) — Campos Actuales + Por Fase

```python
class AgentState(TypedDict, total=False):
    # Sesión y tracking
    session_id: UUID
    user_id: UUID
    mode: AgentMode
    agent_run_id: UUID | None
    current_phase: str | None

    # Conversación
    messages: list[LLMMessage]
    user_input: str
    response: str

    # Documentos
    document_text: str | None
    document_metadata: dict
    extracted_data: dict
    classification: str
    justification: str

    # Google Workspace / Evidencia (Fases 0–4)
    contrato_contexto: dict        # {fecha_inicio, fecha_fin, entidad, supervisor_email}
    obligaciones_contexto: list[dict]
    email_evidence: list[dict]
    email_message_ids: list[str]
    drive_folder_id: str | None
    drive_file_ids: list[str]
    actividades_generadas: list[dict]

    # Onboarding SECOP (Fase 1)
    cedula: str | None
    secop_contratos: list[dict] | None
    secop_documentos: list[dict] | None
    uploaded_file_ids: list[UUID] | None
    onboarding_mode: str | None    # "secop" | "manual"

    # Entidad y plantillas (Fase 2)
    entity_requirements: dict | None
    entity_profile_id: UUID | None
    template_id: UUID | None
    document_type: str | None
    hil_reason: str | None

    # Calidad (Fase 3)
    quality_gate_passed: bool | None
    quality_issues: list[str] | None

    # Evidencia orquestada (Fase 4)
    evidence_raw: list[dict] | None
    local_evidence: list[dict] | None
    matched_evidence: dict[str, list[dict]] | None
    deduplicated_evidence: list[dict] | None

    # Documentos ensamblados (Fase 5)
    document_drafts: list[dict] | None
    preview_html: str | None
    preview_approved: bool | None
    folder_manifest: dict[str, str] | None

    # Supervisor + HIL (Fase 6)
    supervisor_plan: list[str] | None
    borrador_version: int | None
    human_review_pending: bool | None

    # Control
    error: str | None

    # Non-checkpointable (prefijo underscore)
    _db: Any
    _pdf_bytes: Any
```

### Optimización de Tokens por Tarea

| Tarea                    | Modelo recomendado           | Por qué                              |
| ------------------------ | ---------------------------- | ------------------------------------ |
| Routing (1 palabra)      | `groq/llama-3.1-8b-instant`  | Ultra-rápido, temp 0.0               |
| Extracción obligaciones  | `gemini/gemini-2.5-flash`    | Contexto largo, documentos extensos  |
| Email matching           | `groq/llama-3.1-8b-instant`  | Clasificación binaria, barato        |
| Justificación / narrativa| `gemini/gemini-2.5-flash`    | Mejor calidad de lenguaje            |
| Dev / testing            | `ollama/qwen2.5:7b`          | Gratis, sin API key                  |

### MCP Server Pattern

```python
# mcp_servers/gmail_server.py
from mcp.server import Server
from mcp.server.stdio import stdio_server

app = Server("gmail-server")

@app.list_tools()
async def list_tools(): ...      # Declara herramientas con JSON schema

@app.call_tool()
async def call_tool(name, args): # Proxea a FastAPI backend

async def main():
    async with stdio_server() as streams:
        await app.run(*streams, app.create_initialization_options())
```

### Retry Logic

- 2 reintentos con backoff exponencial (1-4s)
- Fallback automático: default → fallback → local → RuntimeError

---

## 4. Seguridad y Auth

### Competencias

- JWT HS256: access tokens (15min) + refresh tokens (7 días)
- Bcrypt cost 12 para hashing de passwords
- Token blacklist para revocación inmediata
- Brute force protection: lock después de 10 intentos
- Refresh tokens de un solo uso con rotación
- OAuth 2.0 + PKCE para Google Workspace (futuro)
- Fernet encryption para tokens OAuth en reposo

### Patrones de Seguridad Implementados

```python
# Dependency de auth
async def get_current_user(request: Request, db: AsyncSession) -> Usuario:
    # 1. Extract Bearer token
    # 2. Decode JWT, verify exp/type
    # 3. Check JTI not blacklisted
    # 4. Query user, verify active
    # 5. Set request.state.user_id

# RBAC factory
def require_role(allowed_roles: list[str]):
    async def dependency(user: CurrentUser) -> Usuario:
        if user.rol.value not in allowed_roles:
            raise ForbiddenError()
        return user
    return Depends(dependency)

# Credit check
async def require_credits(amount: int, user: CurrentUser) -> Usuario:
    if user.creditos_disponibles < amount:
        raise InsufficientCreditsError(amount, user.creditos_disponibles)
    return user
```

### Validación de Archivos

- MIME type check con magic bytes (`python-magic`)
- Extension whitelist: pdf, jpg, jpeg, png, docx, xlsx, pptx, xls
- Path traversal prevention: sanitize filename
- No doble extensiones (ej: `.pdf.exe`)
- Max 10MB por archivo

---

## 5. Testing Async

### Competencias

- `pytest-asyncio` con `asyncio_mode = "auto"`
- `httpx.AsyncClient` con ASGI transport (sin servidor HTTP)
- SQLite in-memory (`aiosqlite`) como DB de test
- `moto[s3]` para mocking de storage
- `factory-boy` para generación de datos
- Rate limiter deshabilitado en tests

### Estructura de Tests

```python
# conftest.py fixtures
@pytest.fixture
async def db():      # AsyncSession aislada con create/drop per test

@pytest.fixture
async def client():  # AsyncClient con app FastAPI

@pytest.fixture
async def test_user():  # Usuario + JWT token pre-creado
```

### Cobertura

- Mínimo: 70% (fail_under en pyproject.toml)
- Tests unitarios para reglas de negocio
- Tests de integración para endpoints HTTP
- Tests del agente: parsers, prompts, schemas, LLM adapter

---

## 6. Dominio de Negocio (Colombia)

### Competencias

- Cuentas de cobro para contratos de prestación de servicios
- Máquina de estados: `borrador → enviada → en_revision → aprobada → pagada`
- Clasificación de actividades: LABORAL / NO_LABORAL / PARCIAL
- Obligaciones contractuales (general/específica)
- Justificaciones formales en español colombiano profesional
- Gestión de evidencias (correos, calendarios, archivos Drive)
- Créditos como moneda interna (sistema prepago)

### Reglas Financieras

- `Decimal` para todo monto, nunca `float`
- Redondeo explícito en operaciones monetarias
- Idempotencia en webhooks de pago (Wompi)
- Conciliación de estados de pago

---

## 7. Observabilidad

### Competencias

- Structlog: JSON en prod, consola en dev
- Trace ID por request (UUID en `request.state.trace_id`)
- Audit events: login, logout, upload, permissions
- Error responses con trace_id para debugging
- Request logging: method, path, status, user_id, IP, user-agent

### Patrón

```python
logger = structlog.get_logger("service.auth")
await logger.ainfo("user_registered", email=email, credits=30)
await logger.aerror("login_failed", email=email, attempts=attempts)
```

---

## 8. Storage y Document Processing

### Competencias

- S3-compatible storage vía `StoragePort` Protocol
- Buckets separados: documentos, evidencias, PDFs
- Key structure: `usuarios/{user_id}/documentos/{doc_id}/{filename}`
- Document parsing: PDF (pdfplumber + OCR fallback con pytesseract), DOCX (python-docx), XLSX (openpyxl)
- PDF generation: HTML → WeasyPrint con Jinja2 templates
- DOCX generation: python-docx con llenado de plantillas `{{campo}}`
- OCR para contratos escaneados: pdf2image → pytesseract (lang=spa)
- Evidence folder organization per billing period

### Patrón Storage

```python
# Storage via protocol (never direct boto3)
class StoragePort(Protocol):
    async def upload(self, key: str, data: bytes, content_type: str) -> str: ...
    async def download(self, key: str) -> bytes: ...
    async def presigned_url(self, key: str, expires_in: int) -> str: ...
    async def delete(self, key: str) -> None: ...
```

---

## 9. Google Workspace Integration

### Competencias

- OAuth 2.0 Authorization Code flow con `google-auth-oauthlib`
- Gestión de tokens: encrypt (Fernet) → DB → decrypt → auto-refresh
- Gmail API: búsqueda con query strings, parsing de MIME multipart, envío con adjuntos
- Drive API: CRUD de archivos/carpetas, slugificación de nombres, presigned share links
- Calendar API: listado de eventos por rango temporal
- Construcción de queries Gmail optimizadas a partir de texto de obligaciones
- Matching semántico email ↔ obligación con LLM (temp=0.0)
- Todas las llamadas a Google son síncronas → siempre `asyncio.run_in_executor`

### OAuth Flow

```
1. GET /integraciones/google/connect          → devuelve auth_url
2. Usuario visita auth_url → autoriza scopes
3. GET /integraciones/google/callback?code=X  → intercambia code por tokens
4. Tokens cifrados con Fernet, guardados en google_tokens
5. Cada llamada API: cargar tokens → verificar expiry → refresh si necesario
```

### Scopes requeridos (mínimos)

```python
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",   # solo archivos creados por la app
    "https://www.googleapis.com/auth/calendar.readonly",
    "openid", "email", "profile",
]
```

### Regla crítica

`prompt="consent"` es obligatorio en `authorization_url()` — sin esto, Google no devuelve `refresh_token` en reconexiones y los tokens expiran sin posibilidad de renovación.

---

## 10. CrewAI — Subagentes Paralelos

### Competencias

- Diseño de Crews con agentes especializados de responsabilidad única
- `kickoff_async()` para integración no-bloqueante con LangGraph
- Herramientas como `@tool` decorators (funciones puras)
- Aislamiento en `app/agent/crews/` — tests propios, sin mezclar con graph.py
- Pinning estricto de `langchain-core` para evitar conflictos con LangGraph

### Patrón de Crew

```python
# app/agent/crews/evidence_crew.py
from crewai import Agent, Crew, Task

gmail_agent = Agent(
    role="Gmail Evidence Specialist",
    goal="Find emails relevant to contractual obligations",
    backstory="...",
    tools=[search_gmail_tool],
    llm="groq/llama-3.1-8b-instant",
    verbose=False,
)

gather_task = Task(
    description="Search Gmail for evidence of obligation: {obligation_text}",
    expected_output="JSON list of relevant email IDs with relevance scores",
    agent=gmail_agent,
)

evidence_crew = Crew(
    agents=[gmail_agent, drive_agent, calendar_agent],
    tasks=[gather_task, drive_task, calendar_task],
    process=Process.parallel,
)
```

### Reglas

1. Usar `Process.parallel` cuando los agentes son independientes
2. Timeout máximo: 120 segundos por crew
3. Si falla un agente → log warning, continuar con los demás
4. No importar CrewAI fuera de `app/agent/crews/`
5. Tests: mockear `kickoff_async` en tests de nodos LangGraph

---

## 11. pgvector — Búsqueda Semántica

### Competencias

- Instalación pgvector en PostgreSQL (extensión `vector`)
- Columna `embedding VECTOR(768)` en tablas `obligaciones`, `evidencias`
- Generación de embeddings con `text-embedding-004` (Google) via LiteLLM
- Búsqueda por cosine similarity: `<=>` operator
- Índice HNSW para performance en grandes volúmenes

### Patrón

```python
# Migración Alembic
op.execute("CREATE EXTENSION IF NOT EXISTS vector")
op.add_column("obligaciones", sa.Column("embedding", Vector(768), nullable=True))
op.create_index("ix_obligaciones_embedding", "obligaciones", ["embedding"],
                postgresql_using="hnsw",
                postgresql_with={"m": 16, "ef_construction": 64},
                postgresql_ops={"embedding": "vector_cosine_ops"})

# Búsqueda semántica
result = await db.execute(
    select(Obligacion)
    .order_by(Obligacion.embedding.cosine_distance(query_embedding))
    .limit(5)
)
```

---

## 12. SECOP II — Contratos Públicos Colombia

### Competencias

- API Socrata (data.gov.co): datasets `jbjy-vk9h` (contratos) y `p6dx-8zbt` (documentos)
- Filtros: `proveedor_identificacion`, `estado=vigente`, `tipo_contrato`
- Paginación: `$offset` + `$limit` (máx 1000 por request)
- Manejo de timeouts y rate limits del API público (sin API key requerida)

### Patrón

```python
# tools/secop_client.py
import httpx

SECOP_CONTRATOS_URL = "https://www.datos.gov.co/resource/jbjy-vk9h.json"
SECOP_DOCUMENTOS_URL = "https://www.datos.gov.co/resource/p6dx-8zbt.json"

async def buscar_contratos_por_cedula(cedula: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        params = {"proveedor_identificacion": cedula, "$limit": 100, "$offset": 0}
        response = await client.get(SECOP_CONTRATOS_URL, params=params)
        response.raise_for_status()
        return response.json()
```

---

## 13. Langfuse — Observabilidad LLM

### Competencias

- SDK Python: `from langfuse import Langfuse`
- Instrumentación de LiteLLM via callback: `litellm.callbacks = [LangfuseCallback()]`
- Trazas por `thread_id` (= `conversacion.id`) para correlación con LangGraph
- Scores automáticos del judge: `langfuse.score(trace_id=..., name="quality", value=0.9)`
- Self-hosted en Railway (Docker): `langfuse/langfuse:latest`

### Patrón de inicialización

```python
# app/core/config.py
langfuse = Langfuse(
    public_key=settings.LANGFUSE_PUBLIC_KEY,
    secret_key=settings.LANGFUSE_SECRET_KEY,
    host=settings.LANGFUSE_HOST,  # self-hosted URL
)
```
