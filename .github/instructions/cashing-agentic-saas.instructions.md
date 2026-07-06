---
description: "Use when building, extending, or designing any feature of CashIn: agentic SaaS, LangGraph nodes, FastAPI agent endpoints, evidence gathering, obligation matching, document generation, HIL (Human-in-the-Loop), SaaS multi-tenancy, credits, subscriptions, MCP tools, Ports & Adapters, Supabase-free PostgreSQL, Alembic migrations, LiteLLM, Ollama, Google Workspace OAuth, cuenta de cobro, invoice generation, contratistas colombianos, agentic architecture, tool building, agent memory, agent tracing, LangGraph state, agent modes, PIPELINE EVIDENCE CHAT DRIVE CONFIG EXTRACT_OBLIGATIONS GENERATE_ACTIVITIES SECOP_DISCOVERY REQUIREMENTS_INGESTION TEMPLATE_RESOLVE QUALITY_GATE CUENTA_COBRO_FULL, CrewAI, pgvector, SECOP II, Langfuse, checkpointing, evidence orchestration, document assembly, onboarding."
applyTo: "**"
---

# CashIn — Instrucciones de Arquitectura Agéntica SaaS

> Documento rector para decisiones de diseño y desarrollo en CashIn.
> Prevalece sobre sugerencias genéricas de Copilot/Claude.
> Complementa `.github/INSTRUCTIONS.md`, `AGENTS.md`, `SKILLS.md`, `TOOLS.md`.

---

## 0. Filosofía Central: Agent-First, SaaS-Ready

CashIn no es una app CRUD que "también tiene IA". Es un **agente razonador** al que se accede como servicio. Cada feature nueva debe plantearse como:

> _"¿Qué nodo LangGraph resuelve esto? ¿Qué herramienta necesita el agente? ¿Dónde interviene el humano?"_

Nunca como:
> _"¿Qué endpoint CRUD necesito para guardar esto en la base de datos?"_

**Los endpoints FastAPI son puertas de acceso al agente, no la lógica en sí.**

---

## 1. Arquitectura Agéntica — Reglas de Diseño

### 1.1 Grafo LangGraph (Fuente de Verdad del Comportamiento)

El grafo en `app/agent/graph.py` es el corazón del sistema. Toda intención del usuario pasa por el `router_node` antes de enrutarse.

**Modos actuales y cuándo usarlos:**

| Modo | Nodos | Caso de Uso | Estado |
|------|-------|------------|--------|
| `CHAT` | `router → chat` | Preguntas conversacionales, ayuda contextual | ✅ |
| `PIPELINE` | `router → doc_ingestion → doc_understanding → classification → justification` | Procesar un contrato subido por el usuario | ✅ |
| `EVIDENCE` | `router → email_fetch → (obligation_matching) → END` | Recolectar evidencia de Gmail para un período | ✅ |
| `DRIVE` | `router → drive_upload` | Subir documento final a Google Drive | ✅ |
| `EXTRACT_OBLIGATIONS` | `router → extraction_router → contract_metadata? → obligations_extraction` | Extraer obligaciones de texto de contrato | ✅ |
| `GENERATE_ACTIVITIES` | `router → generate_activities` | Generar lista de actividades para cuenta de cobro | ✅ |
| `CONFIG` | `router → chat` | Configuración conversacional del agente (en evolución) | ✅ |
| `SECOP_DISCOVERY` | `router → secop_discovery` | Detectar contratos en SECOP II por cédula | 🔲 Fase 1 |
| `REQUIREMENTS_INGESTION` | `router → requirements_ingestion → entity_profile` | Parsear guías de entidad → `EntityRequirements` | 🔲 Fase 2 |
| `TEMPLATE_RESOLVE` | `router → template_resolver → (HIL si falta)` | Resolver/solicitar plantilla para un tipo de documento | 🔲 Fase 2 |
| `QUALITY_GATE` | `router → quality_gate` | Validar obligaciones/evidencias extraídas (LLM judge) | 🔲 Fase 3 |
| `CUENTA_COBRO_FULL` | `supervisor → [todos los nodos de cada fase]` | **Orquestador maestro: flujo end-to-end completo** | 🔲 Fase 6 |

**Para agregar un nuevo modo:**
1. Agregar valor a `AgentMode` enum en `app/schemas/agent.py`
2. Crear nodo(s) en `app/agent/nodes/{nuevo_modo}.py`
3. Actualizar `_route_by_mode()` en `graph.py`
4. Agregar edges en `build_graph()`
5. Actualizar `AgentState` en `state.py` con campos específicos del modo
6. Escribir prompts en `app/agent/prompts/{nuevo_modo}.py`

### 1.2 Estado del Agente (`AgentState`)

`AgentState` es `TypedDict(total=False)`. Reglas estrictas:

```python
# ✅ Campos serializables (checkpointables) — primitivos, listas, dicts
session_id: uuid.UUID
email_evidence: list[dict[str, str]] | None

# ✅ Campos no serializables — prefijo underscore, solo en memoria
_db: Any          # AsyncSession — NUNCA checkpointear
_pdf_bytes: Any   # bytes crudos — NUNCA checkpointear

# ✅ Nodos retornan solo los campos que modifican (spread parcial)
def my_node(state: AgentState) -> dict:
    return {"response": built_response, "mode": AgentMode.CHAT}

# ❌ NUNCA retornar el estado completo
def my_node(state: AgentState) -> AgentState:  # INCORRECTO
    state["response"] = ...
    return state
```

### 1.3 Nodos: Contrato de Implementación

Cada nodo en `app/agent/nodes/` debe:
- Ser una función async pura: `async def my_node(state: AgentState) -> dict`
- No tener side effects fuera del estado (excepto logs estructurados)
- Acceder a DB/Storage **solo vía `state["_db"]`** inyectado por el service
- Lanzar excepciones de dominio de `app/core/exceptions.py`
- Tener su propio archivo de prompts en `app/agent/prompts/`
- Ser testeable sin servidor HTTP ni LLM real (mocks de LiteLLM)

### 1.4 Temperaturas y Modelos por Tipo de Tarea

```python
# Clasificación/routing → determinista
model = "groq/llama-3.1-8b-instant", temperature = 0.0

# Extracción estructurada → precisa
model = "gemini/gemini-2.5-flash", temperature = 0.1

# Narrativa/justificación → natural pero consistente
model = "gemini/gemini-2.5-flash", temperature = 0.3

# Dev/testing → gratuito local
model = "ollama/qwen2.5:7b", temperature = 0.1

# Fallback chain via LiteLLM (configurar en app/adapters/llm/litellm_adapter.py)
# primario → fallback → ollama (siempre disponible en dev)
```

---

## 2. Diseño SaaS — Reglas de Productización

### 2.1 Multi-tenancy

Todo registro en la base de datos que pertenece a un usuario DEBE tener `usuario_id` con índice y RLS (Row Level Security) en la query, aunque sea PostgreSQL custom y no Supabase.

```python
# ✅ Siempre filtrar por usuario en queries
stmt = select(Contrato).where(
    Contrato.usuario_id == current_user.id,
    Contrato.deleted_at.is_(None)
)

# ❌ NUNCA consultar sin contexto de usuario (excepto admin)
stmt = select(Contrato)  # INSEGURO
```

Ownership check: verificar en el service antes de operar sobre cualquier recurso.

```python
# En service layer
contrato = await get_contrato_or_404(db, contrato_id)
if contrato.usuario_id != current_user.id:
    raise ForbiddenError("Contrato", contrato_id)
```

### 2.2 Sistema de Créditos

El modelo de monetización es **créditos por operación agéntica**. Cada modo del agente tiene un costo en créditos configurado en `app/core/config.py`.

```python
# Costos actuales (ajustar según LLM costs)
CREDITS_PER_CHAT_MESSAGE: int = 1
CREDITS_PER_PIPELINE_RUN: int = 5      # Procesa contrato completo
CREDITS_PER_EVIDENCE_RUN: int = 3      # Gmail search + matching
CREDITS_PER_ACTIVITIES_GENERATION: int = 2
CREDITS_PER_DOCUMENT_GENERATION: int = 5  # PDF/DOCX final

# Guardar siempre: fecha, modo, créditos usados, resultado
```

**Flujo de créditos:**
1. `require_credits(n)` — dependency en FastAPI que verifica antes del agente
2. Al completar: `CreditoService.consume(db, usuario_id, n, descripcion)`
3. En error del agente: **no consumir** créditos (rollback implícito)

### 2.3 Suscripciones (Wompi)

Las suscripciones determinan el techo de créditos y features disponibles:

| Plan | Créditos/mes | Features |
|------|-------------|----------|
| `FREE` | 20 | Chat + Pipeline básico |
| `PRO` | 200 | Todo + Drive + Calendar |
| `ENTERPRISE` | ilimitado | Multi-contrato + API key propia |

Las subscripciones se gestionan vía Wompi (pasarela colombiana). Los webhooks de Wompi actualizan `suscripciones.estado` y recargan créditos.

### 2.4 Rate Limiting

- Endpoints públicos (auth, webhook): 10 req/min por IP via `slowapi`
- Endpoints autenticados: 60 req/min por usuario
- Endpoints del agente (SSE): 5 streams simultáneos por usuario
- Configurar en `app/core/rate_limit.py`

---

## 3. Ports & Adapters — Reglas de Integración

### 3.1 Principio de Aislamiento

El core nunca sabe qué proveedor está detrás de un puerto. Esta regla es absoluta:

```
app/services/    → usa Protocol (Port)        ✅
app/agent/nodes/ → usa Protocol (Port)        ✅
app/adapters/    → implementa Protocol        ✅
app/api/         → inyecta adaptador concreto ✅

app/services/    → import boto3               ❌
app/agent/nodes/ → import googleapiclient     ❌
```

### 3.2 Añadir una Nueva Integración

Checklist obligatorio para cualquier integración externa (API, servicio, proveedor):

```
1. app/adapters/{servicio}/port.py          → Protocol con type hints completos
2. app/adapters/{servicio}/{impl}_adapter.py → Implementación concreta
3. app/services/{servicio}_service.py       → Lógica de negocio usando el Port
4. app/api/v1/{servicio}.py                 → Endpoints FastAPI (OAuth si aplica)
5. mcp_servers/{servicio}_server.py         → MCP server standalone
6. .claude/settings.json                    → Registrar el MCP server
7. alembic/versions/XXX_{servicio}.py       → Migración si hay nuevas tablas
8. tests/test_{servicio}_*.py               → Tests unitarios + integración
```

### 3.3 Google APIs — Reglas Especiales

Las Google APIs son **síncronas** en el SDK Python. Siempre usar `run_in_executor`:

```python
import asyncio
from functools import partial

# ✅ Correcto
result = await asyncio.get_event_loop().run_in_executor(
    None,
    partial(service.users().messages().list(userId="me", q=query).execute)
)

# ❌ Incorrecto — bloquea el event loop
result = service.users().messages().list(userId="me", q=query).execute()
```

Tokens OAuth: **siempre cifrar con Fernet antes de persistir** en `google_tokens`.

```python
from cryptography.fernet import Fernet
cipher = Fernet(settings.FERNET_KEY)
encrypted = cipher.encrypt(token_json.encode()).decode()
# Guardar `encrypted`, no `token_json`
```

---

## 4. MCP Servers — Reglas de Desarrollo

Los MCP servers en `mcp_servers/` son **procesos independientes** del servidor FastAPI. Son la interfaz entre Claude/Copilot y los servicios de Google Workspace.

### 4.1 Principio de Proxy

Los MCP servers **no llaman a Google APIs directamente**. Llaman a la API FastAPI con un token de servicio interno:

```python
# ✅ MCP server → FastAPI API → GmailAdapter → Gmail API
async def search_emails(query: str, user_id: str) -> list[dict]:
    response = await http_client.get(
        f"{CASHIN_API_URL}/api/v1/integraciones/gmail/search",
        params={"q": query, "user_id": user_id},
        headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}
    )
    return response.json()

# ❌ MCP server → Gmail API directamente
import googleapiclient  # NO en mcp_servers/
```

### 4.2 Herramientas Disponibles por Servidor

```python
# gmail_server.py
@server.tool("search_emails")      # Busca emails por query y rango de fechas
@server.tool("get_email_content")  # Obtiene cuerpo completo de un email
@server.tool("send_email")         # Envía cuenta de cobro como email

# drive_server.py
@server.tool("upload_file")        # Sube PDF/DOCX a carpeta del período
@server.tool("list_files")         # Lista archivos en carpeta del contrato
@server.tool("create_folder")      # Crea estructura de carpetas del período
@server.tool("make_shareable")     # Genera link compartible

# calendar_server.py
@server.tool("list_events")        # Lista eventos del período del contrato
@server.tool("get_event_details")  # Detalles de una reunión (evidencia)
```

### 4.3 Agregar Nuevas Herramientas

Cuando el agente necesite una capacidad nueva:
1. Evaluar si se resuelve con un **nodo LangGraph** (procesamiento interno) o un **MCP tool** (acción en sistema externo)
2. Si es MCP: agregar `@server.tool()` en el servidor correspondiente, exponiendo el endpoint en FastAPI
3. Si es nodo: seguir el contrato del §1.3

---

## 5. Generación de Documentos — Pipeline

El pipeline de generación es el producto final del agente. Sigue esta secuencia:

```
AgentState (justificación, actividades, obligaciones, datos del contratista)
  → DocumentService.build_cuenta_cobro_context(state)     # Ensambla el contexto
  → PlantillaService.render_template(context, plantilla_id) # Jinja2 → HTML
  → PDFService.html_to_pdf(html)                            # WeasyPrint → bytes
  → DOCXService.generate_docx(context, template_path)       # python-docx
  → StorageService.upload(pdf_bytes, s3_key)                # S3/R2/MinIO
  → DriveService.upload_file(pdf_bytes, folder_id)          # Google Drive
  → CuentaCobroService.update_estado(id, "generado", urls)  # Persistir URLs
```

**Reglas del pipeline:**
- Siempre generar **ambos formatos** (PDF y DOCX) si el plan del usuario lo permite
- El HTML intermedio debe ser válido para WeasyPrint (usar CSS específico para print)
- Las plantillas en `app/static/templates/` son archivos `.html.j2` con variables Jinja2
- Los documentos finales se persisten en S3/R2 y el path se guarda en `cuentas_cobro.documento_url`
- Nombres de archivo: `{año}-{mes:02d}_{contrato_referencia}_{timestamp}.pdf`

---

## 6. Human-in-the-Loop (HIL) — Patrones de Revisión

El agente puede pausar y esperar intervención humana. Esto es crítico para la cuenta de cobro (el usuario debe aprobar antes de enviar).

### 6.1 Puntos de Interrupción Actuales

| Punto | Qué espera el usuario | Acción API |
|-------|-----------------------|------------|
| Después de `justification` | Revisar borrador de justificación | `PATCH /api/v1/agent/sessions/{id}/feedback` |
| Después de `generate_activities` | Aprobar/editar lista de actividades | `PATCH /api/v1/agent/sessions/{id}/approve-activities` |
| Antes de `drive_upload` | Confirmar subida a Drive | `POST /api/v1/agent/sessions/{id}/confirm-upload` |

### 6.2 Patrón de Implementación HIL

```python
# En el nodo que requiere aprobación humana:
def justification_node(state: AgentState) -> dict:
    justification = await llm.complete(prompt, context)
    return {
        "justification": justification,
        "awaiting_human_approval": True,   # Flag en AgentState
        "human_approval_type": "justification"
    }

# El service detecta el flag y persiste el estado
# El endpoint /feedback reanuda desde el checkpoint

# Cuando llega el feedback del usuario:
def apply_feedback_node(state: AgentState) -> dict:
    refined = await llm.complete(
        REFINE_PROMPT.format(
            original=state["justification"],
            feedback=state["user_feedback"]
        )
    )
    return {"justification": refined, "awaiting_human_approval": False}
```

### 6.3 Streaming SSE

Las respuestas del agente se envían como **Server-Sent Events** para UX en tiempo real:

```python
# En api/v1/agent.py
@router.post("/sessions/{session_id}/chat")
async def chat_stream(session_id: UUID, request: ChatRequest):
    async def event_generator():
        async for chunk in agent_service.stream(session_id, request):
            yield f"data: {chunk.model_dump_json()}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

---

## 7. Observabilidad Agéntica

### 7.1 Logging con structlog

Todo log debe incluir contexto del agente:

```python
import structlog
logger = structlog.get_logger("agent.nodes.email_fetch")

# ✅ Log enriquecido
logger.info(
    "email_fetch.completed",
    session_id=str(state["session_id"]),
    user_id=str(state["user_id"]),
    emails_found=len(emails),
    query=state["email_query"],
    duration_ms=elapsed,
)

# ❌ Log sin contexto
logger.info("Found emails")
```

### 7.2 Métricas del Agente

Registrar en cada ejecución:
- `tokens_used`: tokens consumidos por el LLM (desde LiteLLM response)
- `duration_ms`: tiempo de ejecución del nodo
- `model_used`: modelo efectivamente usado (puede diferir por fallback)
- `credits_consumed`: créditos deducidos al usuario

Persitir en tabla `agent_runs` con estructura:
```sql
agent_runs (
  id UUID, session_id UUID, user_id UUID,
  mode TEXT, node TEXT,
  tokens_used INT, duration_ms INT, model_used TEXT,
  credits_consumed INT, status TEXT,
  created_at TIMESTAMPTZ
)
```

### 7.3 Trazabilidad de Decisiones

El agente debe poder **explicar sus decisiones**. El campo `reasoning` en tablas de matching/clasificación es obligatorio — no es opcional para el MVP.

```python
# ✅ Siempre incluir razonamiento
{
    "obligacion_id": "...",
    "email_id": "...",
    "clasificacion": "LABORAL",
    "confidence": 0.92,
    "reasoning": "El email menciona explícitamente la entrega del informe mensual solicitado en la obligación 3."
}
```

---

## 8. Modelos de Datos — Evolución

### 8.1 Convenciones de Modelos ORM

```python
# ✅ Siempre heredar los tres mixins
class MiModelo(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "mi_tabla"

# ✅ Soft delete — nunca DELETE físico en producción
await db.execute(
    update(MiModelo)
    .where(MiModelo.id == id)
    .values(deleted_at=datetime.utcnow())
)

# ✅ Nombres de tabla en plural, español, snake_case
__tablename__ = "cuentas_cobro"   # ✅
__tablename__ = "CuentaCobro"     # ❌
__tablename__ = "billing_period"  # ❌ (mezcla idiomas)
```

### 8.2 Tablas Pendientes de Implementar

Estas tablas están planeadas y deben seguir las convenciones del §8.1:

```sql
-- Ejecuciones del agente (observabilidad)
CREATE TABLE agent_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES conversaciones(id),
    usuario_id UUID NOT NULL REFERENCES usuarios(id),
    modo TEXT NOT NULL,
    nodo TEXT,
    tokens_usados INTEGER,
    duracion_ms INTEGER,
    modelo_usado TEXT,
    creditos_consumidos INTEGER DEFAULT 0,
    estado TEXT DEFAULT 'completado',  -- completado | fallido | en_progreso
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Evidencia recopilada por el agente (para trazabilidad)
CREATE TABLE evidencias (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cuenta_cobro_id UUID REFERENCES cuentas_cobro(id),
    fuente TEXT NOT NULL,          -- 'gmail' | 'drive' | 'calendar' | 'manual'
    fuente_id TEXT,                -- ID del email/archivo en el proveedor
    contenido TEXT NOT NULL,       -- Texto extraído
    embedding VECTOR(1536),        -- pgvector para búsqueda semántica futura
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Versiones de borradores (HIL)
CREATE TABLE borradores_cuenta_cobro (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cuenta_cobro_id UUID REFERENCES cuentas_cobro(id),
    version INTEGER DEFAULT 1,
    contenido TEXT NOT NULL,          -- Markdown/HTML del borrador
    feedback_usuario TEXT,            -- Input del usuario para refinar
    aprobado BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 8.3 pgvector — Búsqueda Semántica

Para la tabla `evidencias.embedding`, habilitar la extensión:

```sql
-- Migración Alembic
CREATE EXTENSION IF NOT EXISTS vector;
-- Índice para búsqueda ANN eficiente
CREATE INDEX ON evidencias USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

Uso en queries:
```python
# Búsqueda semántica de evidencia similar a una obligación
stmt = (
    select(Evidencia)
    .order_by(Evidencia.embedding.cosine_distance(query_embedding))
    .limit(5)
)
```

---

## 9. Testing Agéntico

### 9.1 Tests de Nodos (Unitarios)

```python
# tests/agent/test_email_fetch_node.py
async def test_email_fetch_finds_relevant_emails(mock_gmail_adapter):
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        contrato_contexto={"entidad": "ICBF", "fecha_inicio": "2026-01-01"},
        obligaciones_contexto=[{"descripcion": "Reunión mensual de seguimiento"}],
        _db=AsyncMock(),
    )
    mock_gmail_adapter.search_messages.return_value = [fake_email]

    result = await email_fetch_node(state)

    assert "email_evidence" in result
    assert len(result["email_evidence"]) > 0
```

### 9.2 Tests de Integración del Agente

```python
# tests/test_agent_pipeline.py — test end-to-end del grafo completo
async def test_pipeline_mode_full_flow(client, test_user, mock_llm, mock_storage):
    # 1. Subir documento de contrato
    resp = await client.post("/api/v1/agent/upload", files={"file": pdf_bytes})
    session_id = resp.json()["session_id"]

    # 2. Ejecutar el agente en modo PIPELINE
    resp = await client.post(f"/api/v1/agent/sessions/{session_id}/run",
                             json={"mode": "PIPELINE"})
    assert resp.status_code == 200

    # 3. Verificar que se extrajeron obligaciones
    state = await client.get(f"/api/v1/agent/sessions/{session_id}/state")
    assert state.json()["obligaciones_extraidas"] is not None
```

### 9.3 Mocks Obligatorios

```python
# conftest.py — fixtures reutilizables
@pytest.fixture
def mock_llm():
    """Mock LiteLLM para tests sin costo ni latencia."""
    with patch("app.adapters.llm.litellm_adapter.completion") as m:
        m.return_value = fake_llm_response(content="respuesta del LLM")
        yield m

@pytest.fixture
def mock_gmail():
    """Mock GmailAdapter para tests sin OAuth."""
    with patch("app.adapters.email.gmail_adapter.GmailAdapter") as m:
        m.return_value.search_messages.return_value = []
        yield m
```

---

## 10. Escalabilidad SaaS — Consideraciones Futuras

### 10.1 Horizontalidad del Agente

El agente LangGraph es **stateless por diseño** — el estado se serializa y puede almacenarse externamente. Para escalar:
- Usar **LangGraph Checkpointer** con PostgreSQL (ya disponible: `langgraph-checkpoint-postgres`)
- Esto permite múltiples workers con el mismo grafo
- Las sesiones de usuario se mapean a `thread_id` del checkpointer

### 10.2 Cola de Trabajos (Fase 2)

Para operaciones largas (pipeline completo, evidence gathering masivo):
```
FastAPI endpoint → Encolar en Redis/RQ → Worker agéntico → SSE de progreso
```
Usar `arq` (async task queue) para workers asincrónicos con Python.

### 10.3 API Keys para Enterprise

Los clientes Enterprise recibirán su propia API key para acceso programático:
```
X-CashIn-API-Key: cashin_live_xxxxx
```
- Generadas con `secrets.token_urlsafe(32)` + prefijo
- Hasheadas en BD (bcrypt) — nunca guardar en claro
- Asociadas a `usuario_id` con límites de rate per key

### 10.4 White-label (Roadmap)

La arquitectura Ports & Adapters ya soporta white-label:
- Plantillas de documentos configurables por tenant
- Branding (logo, colores) inyectado via Jinja2 context
- Subdomain routing: `{empresa}.cashin.app`

---

## 11. Seguridad — Checklist por Feature

Antes de completar cualquier PR/tarea que toque la API:

- [ ] ¿El endpoint verifica `get_current_user`?
- [ ] ¿El service valida ownership del recurso (`usuario_id == current_user.id`)?
- [ ] ¿Se consumen créditos antes de ejecutar el agente?
- [ ] ¿Los tokens OAuth están cifrados con Fernet?
- [ ] ¿Hay rate limiting en endpoints públicos?
- [ ] ¿El input del usuario pasa por validación Pydantic antes de llegar al agente?
- [ ] ¿Los logs no contienen datos personales ni tokens?
- [ ] ¿Las queries SQL usan bind parameters (sin f-strings)?
- [ ] ¿Los archivos subidos pasan por `file_validation.py` (MIME check)?

---

## 12. Roadmap Agéntico — Próximas Fases

### Fase Actual (MVP)
- [x] LangGraph con CHAT, PIPELINE, EVIDENCE, DRIVE, EXTRACT_OBLIGATIONS, GENERATE_ACTIVITIES
- [x] LiteLLM con fallback Gemini → Groq → Ollama
- [x] Ports & Adapters: LLM, Storage, Gmail, Drive, Calendar
- [x] MCP servers para Google Workspace
- [x] Generación PDF/DOCX con WeasyPrint + python-docx
- [x] Auth JWT custom + OAuth Google
- [x] Créditos básicos + Wompi

### Fase 2 — Razonamiento Mejorado
- [ ] LangGraph Checkpointer con PostgreSQL (persistencia cross-session)
- [ ] Modo `CUENTA_COBRO_COMPLETA`: orquesta EVIDENCE + GENERATE_ACTIVITIES + DOC_GENERATION en un solo flujo
- [ ] Matching semántico de evidencia con pgvector (embeddings)
- [ ] Borrador con HIL: `borradores_cuenta_cobro` + feedback loop
- [ ] Memoria por usuario: el agente recuerda formato preferido, estilo narrativo

### Fase 3 — SaaS Productización
- [ ] Multi-tenant completo con subdomain routing
- [ ] API keys para integraciones Enterprise
- [ ] Dashboard de observabilidad: tokens, costos, accuracy del matching
- [ ] Evaluación automática de calidad de las cuentas de cobro generadas
- [ ] Fine-tuning de prompts por tipo de entidad (SENA, ICBF, Ministerio, etc.)

### Fase 4 — Agente Autónomo
- [ ] Calendar integration: evidencia de reuniones y eventos
- [ ] Modo `AUTONOMOUS`: el agente ejecuta todo el flujo sin intervención humana
- [ ] Notificaciones proactivas: "Tu cuenta de cobro está lista para revisar"
- [ ] Integración SECOP para validar datos del contrato automáticamente

---

## 13. Referencia Rápida de Archivos Clave

| Archivo | Propósito |
|---------|-----------|
| `app/agent/graph.py` | Definición del grafo LangGraph — el corazón del sistema |
| `app/agent/state.py` | `AgentState` TypedDict — todos los campos del estado |
| `app/agent/nodes/` | Un archivo por nodo/modo del agente |
| `app/agent/prompts/` | Prompts organizados por nodo |
| `app/adapters/llm/port.py` | LLMPort Protocol — contrato del LLM |
| `app/adapters/email/port.py` | EmailPort Protocol — contrato de email |
| `app/adapters/drive/port.py` | DrivePort Protocol — contrato de Drive |
| `app/core/config.py` | Settings centralizadas (créditos, modelos, URLs) |
| `app/core/exceptions.py` | Excepciones de dominio → HTTP codes automáticos |
| `mcp_servers/` | Servidores MCP standalone para herramientas del agente |
| `.github/INSTRUCTIONS.md` | Reglas de implementación detalladas |
| `.github/AGENTS.md` | Playbooks por tipo de tarea |
| `context/estado_actual_v1.md` | Estado del proyecto, lluvia de ideas, roadmap |
