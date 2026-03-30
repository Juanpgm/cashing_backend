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

## 3. LangGraph + LiteLLM (Motor IA)

### Competencias

- Diseño de grafos de workflow con nodos tipados
- State management con `TypedDict(total=False)` y spread pattern
- Streaming SSE para respuestas en tiempo real
- Fallback chains: primario → fallback → local
- Prompt engineering en español colombiano profesional
- Token management y cost optimization

### AgentState (Estado del Agente)

```python
class AgentState(TypedDict, total=False):
    session_id: UUID
    user_id: UUID
    mode: AgentMode          # CHAT | PIPELINE | CONFIG
    messages: list[LLMMessage]
    user_input: str
    response: str
    document_id: UUID | None
    document_text: str | None
    document_metadata: dict
    extracted_data: dict
    classification: str
    justification: str
    error: str | None
```

### Tiers de Modelos LLM

| Tier       | Modelo                | Costo             | Uso                         |
| ---------- | --------------------- | ----------------- | --------------------------- |
| Económico  | Gemini 2.0 Flash-Lite | ~$0.075/1M tokens | Clasificación, routing      |
| Balanceado | GPT-4o-mini           | ~$0.15/1M tokens  | Justificaciones, extracción |
| Local      | Ollama Llama 3.1      | $0                | Desarrollo, testing         |

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
- Document parsing: PDF (pdfplumber), DOCX (python-docx), XLSX (openpyxl)
- PDF generation: HTML → WeasyPrint con Jinja2 templates
- Evidence folder organization per billing period

### Patrón

```python
# Storage via protocol (never direct boto3)
class StoragePort(Protocol):
    async def upload(self, key: str, data: bytes, content_type: str) -> str: ...
    async def download(self, key: str) -> bytes: ...
    async def presigned_url(self, key: str, expires_in: int) -> str: ...
    async def delete(self, key: str) -> None: ...
```
