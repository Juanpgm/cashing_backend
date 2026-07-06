---
name: cashing-agentic-dev
description: "Skill rector para desarrollo en CashIn. Úsala cuando: agregar nodo LangGraph, nuevo modo del agente, implementar HIL human-in-the-loop, evidence gathering de Gmail o Calendar, generación de cuenta de cobro (PDF/DOCX), Ports & Adapters nueva integración, MCP tool, migración Alembic, testing agéntico, créditos SaaS, multi-tenancy, suscripciones, pgvector embeddings, checkpointer PostgreSQL, streaming SSE, modo PIPELINE EVIDENCE CHAT DRIVE EXTRACT_OBLIGATIONS GENERATE_ACTIVITIES, observabilidad LangFuse, FastAPI endpoint del agente, esquema Pydantic, modelo ORM, seguridad JWT, OAuth Google, cashing cashin backend, contratista colombiano, cuenta de cobro."
argument-hint: "Describe qué deseas implementar: nodo, modo, integración, test, migración, pipeline de documentos"
---

# CashIn Agentic Dev — Skill de Desarrollo

Skill rector para implementar cualquier feature en CashIn siguiendo el paradigma **Agent-First, SaaS-Ready**. Antes de escribir cualquier línea de código, esta skill determina el camino correcto a través del sistema agéntico.

> Lee [`context/estado_actual_v1.md`](../../../context/estado_actual_v1.md) para el estado actual del proyecto.  
> Consulta [`.github/instructions/cashing-agentic-saas.instructions.md`](../instructions/cashing-agentic-saas.instructions.md) para las reglas de arquitectura.

---

## Decisión Inicial — ¿Qué tipo de feature es?

```
¿La feature involucra razonamiento, acción externa o flujo multi-paso?
  │
  ├── Sí → Nodo LangGraph (o nuevo Modo)
  │           ↓ ver Procedimiento A
  │
  ├── ¿Interacción con Gmail / Drive / Calendar / API externa?
  │     └── Sí → Ports & Adapters + MCP tool
  │                 ↓ ver Procedimiento B
  │
  ├── ¿El usuario necesita revisar y aprobar antes de continuar?
  │     └── Sí → HIL (Human-in-the-Loop)
  │                 ↓ ver Procedimiento C
  │
  ├── ¿Genera un PDF, DOCX o documento final?
  │     └── Sí → Pipeline de Documentos
  │                 ↓ ver Procedimiento D
  │
  └── ¿Es solo almacenamiento o configuración SaaS (créditos, plan)?
        └── Sí → SaaS / Modelo de Datos
                    ↓ ver Procedimiento E
```

---

## Procedimiento A — Agregar Nodo o Modo LangGraph

> Referencia completa: [`./references/node-implementation.md`](./references/node-implementation.md)

### Pasos

**1. Definir el modo en el esquema**
```python
# app/schemas/agent.py → enum AgentMode
class AgentMode(str, Enum):
    MI_NUEVO_MODO = "MI_NUEVO_MODO"
```

**2. Crear el nodo**
```
app/agent/nodes/{nuevo_modo}.py
```
- Firma obligatoria: `async def mi_nodo(state: AgentState) -> dict`
- Solo retorna los campos que modifica
- Accede a DB vía `state["_db"]` — nunca importar Session directamente
- Lanza excepciones de `app/core/exceptions.py`

**3. Crear los prompts**
```
app/agent/prompts/{nuevo_modo}.py
```
- Constantes `SYSTEM_PROMPT`, `USER_PROMPT_TEMPLATE`
- Temperatura según tipo: routing `0.0` | extracción `0.1` | narrativa `0.3`
- Modelo según tarea: `groq/llama-3.1-8b-instant` (routing rápido) | `gemini/gemini-2.5-flash` (extracción/narrativa) | `ollama/qwen2.5:7b` (dev local)

**4. Agregar campos al estado si son necesarios**
```python
# app/agent/state.py → AgentState TypedDict
mi_campo_resultado: list[dict] | None   # checkpointeable
_mi_objeto_pesado: Any                  # prefijo _ = NO checkpointeable
```

**5. Registrar en el grafo**
```python
# app/agent/graph.py
builder.add_node("mi_nodo", mi_nodo)   # en build_graph()
builder.add_conditional_edges(         # en _route_by_mode()
    "router", _route_by_mode,
    {AgentMode.MI_NUEVO_MODO: "mi_nodo", ...}
)
```

**6. Escribir tests**
```
tests/agent/test_{nuevo_modo}_node.py
```
Ver plantilla en [`./references/testing-agentic.md`](./references/testing-agentic.md).

**7. Checklist de validación**
- [ ] El nodo es función `async` pura sin side-effects
- [ ] Los prompts tienen temperatura y modelo explícitos
- [ ] `AgentState` actualizado con nuevos campos
- [ ] Grafo compilado sin errores (`build_graph()` ejecuta en tests)
- [ ] Test unitario del nodo con LLM mockeado
- [ ] Créditos consumidos si aplica (`CREDITS_PER_*` en `config.py`)

---

## Procedimiento B — Nueva Integración (Ports & Adapters + MCP)

> Referencia completa: [`./references/ports-adapters.md`](./references/ports-adapters.md)

### Checklist de 8 pasos (obligatorio)

```
1. app/adapters/{servicio}/port.py           → Protocol con type hints completos
2. app/adapters/{servicio}/{impl}_adapter.py → Implementación concreta
3. app/services/{servicio}_service.py        → Lógica de negocio via Port
4. app/api/v1/{servicio}.py                  → Endpoints FastAPI (OAuth si aplica)
5. mcp_servers/{servicio}_server.py          → MCP server standalone (proxy a FastAPI)
6. .claude/settings.json                     → Registrar el MCP server
7. alembic/versions/XXX_{servicio}.py        → Migración si hay nuevas tablas
8. tests/test_{servicio}_*.py                → Tests unitarios + integración
```

**Regla crítica:** Los MCP servers NO llaman APIs externas directamente. Siempre proxy a FastAPI:
```python
# ✅ mcp_servers/ → FastAPI API → Adapter → API externa
# ❌ mcp_servers/ → API externa directamente
```

**Google APIs específicamente:** Son síncronas — siempre envolver con `run_in_executor`:
```python
result = await asyncio.get_event_loop().run_in_executor(
    None, partial(service.users().messages().list(userId="me", q=query).execute)
)
```

**OAuth tokens:** Siempre cifrar con Fernet antes de persistir en `google_tokens`.

---

## Procedimiento C — HIL (Human-in-the-Loop)

> Referencia completa: [`./references/hil-patterns.md`](./references/hil-patterns.md)

### Pasos

**1. Identificar el punto de pausa** — ¿después de qué nodo el usuario debe aprobar?

**2. Agregar flag de pausa al AgentState**
```python
# app/agent/state.py
awaiting_human_approval: bool | None
human_approval_type: str | None   # "justification" | "activities" | "upload"
user_feedback: str | None
```

**3. El nodo de pausa retorna el flag**
```python
async def justification_node(state: AgentState) -> dict:
    justificacion = await llm_call(...)
    return {
        "justification": justificacion,
        "awaiting_human_approval": True,
        "human_approval_type": "justification"
    }
```

**4. El service persiste el estado en `borradores_cuenta_cobro`**
- Detecta `awaiting_human_approval == True`
- Serializa estado parcial
- Retorna `session_id` al cliente para polling

**5. Endpoint de feedback**
```
PATCH /api/v1/agent/sessions/{session_id}/feedback
Body: { "feedback": "Agrega la reunión del día 15 con el ICBF" }
```
- Carga estado desde checkpointer o BD
- Inyecta `user_feedback` en `AgentState`
- Reanuda el grafo desde el nodo `apply_feedback`

**6. Tabla BD requerida** (Migración Alembic):
```sql
CREATE TABLE borradores_cuenta_cobro (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cuenta_cobro_id UUID REFERENCES cuentas_cobro(id),
    version INTEGER DEFAULT 1,
    contenido TEXT NOT NULL,
    feedback_usuario TEXT,
    aprobado BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Procedimiento D — Pipeline de Generación de Documentos

### Secuencia obligatoria

```python
# 1. Ensamblar contexto desde AgentState
context = DocumentService.build_cuenta_cobro_context(state)

# 2. Renderizar HTML con Jinja2
html = PlantillaService.render_template(context, plantilla_id)
# Plantillas en: app/static/templates/*.html.j2

# 3. Generar PDF con WeasyPrint
pdf_bytes = PDFService.html_to_pdf(html)

# 4. Generar DOCX con python-docx
docx_bytes = DOCXService.generate_docx(context, template_path)

# 5. Subir a S3/R2/MinIO
s3_key = f"{año}/{mes:02d}/{contrato_ref}_{timestamp}.pdf"
url = await StorageService.upload(pdf_bytes, s3_key)

# 6. Subir a Google Drive (si el usuario tiene plan PRO+)
drive_url = await DriveService.upload_file(pdf_bytes, folder_id)

# 7. Persistir URLs en BD
await CuentaCobroService.update_estado(id, "generado", {"pdf_url": url, "drive_url": drive_url})
```

**Naming convention de archivos:**
```
{año}-{mes:02d}_{contrato_referencia}_{timestamp}.pdf
```

**Checklist de documentos:**
- [ ] Generar ambos formatos (PDF + DOCX) si el plan lo permite
- [ ] CSS de WeasyPrint es `@media print` — no CSS de pantalla
- [ ] Variables Jinja2 documentadas en comentario al inicio del `.html.j2`
- [ ] Nombre de archivo incluye timestamp (evitar colisiones en S3)
- [ ] URL del documento persistida en `cuentas_cobro.documento_url`
- [ ] Créditos consumidos: `CREDITS_PER_DOCUMENT_GENERATION = 5`

---

## Procedimiento E — Feature SaaS (Créditos / Multitenancy / Migraciones)

> Referencia completa: [`./references/saas-patterns.md`](./references/saas-patterns.md)

### Multi-tenancy — Regla absoluta

Toda query que toca datos de usuario DEBE filtrar por `usuario_id`:
```python
stmt = select(MiModelo).where(
    MiModelo.usuario_id == current_user.id,
    MiModelo.deleted_at.is_(None)  # soft delete siempre
)
```

Y verificar ownership en el service:
```python
if recurso.usuario_id != current_user.id:
    raise ForbiddenError("Recurso", recurso_id)
```

### Créditos — Flujo estándar

```python
# 1. Verificar ANTES de ejecutar el agente (FastAPI dependency)
@router.post("/agent/run")
async def run_agent(
    credits=Depends(require_credits(CREDITS_PER_PIPELINE_RUN)), ...
):
    ...
# 2. Consumir AL COMPLETAR (no en error)
await CreditoService.consume(db, usuario_id, n, descripcion="Pipeline run")
```

### Convenciones de modelos ORM

```python
class MiModelo(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "mi_tabla"   # plural, español, snake_case
```

### Migración Alembic

```bash
make migrate-create MSG="descripcion_de_cambio"
# Editar el archivo generado en alembic/versions/
# Probar: make migrate  (aplica)
#         make migrate-down  (revierte)
```

---

## Observabilidad — Log Estándar por Nodo

Cada nodo DEBE loguear con `structlog` incluyendo contexto de sesión:

```python
import structlog
logger = structlog.get_logger("agent.nodes.mi_nodo")

logger.info(
    "mi_nodo.completed",
    session_id=str(state["session_id"]),
    user_id=str(state["user_id"]),
    duration_ms=elapsed,
    model_used=model_name,
    tokens_used=tokens,
)
```

---

## Seguridad — Checklist por PR

Antes de completar cualquier tarea que toque la API:

- [ ] ¿El endpoint usa `get_current_user` de `app/api/deps.py`?
- [ ] ¿El service valida `usuario_id == current_user.id`?
- [ ] ¿Se verifican créditos antes de ejecutar el agente?
- [ ] ¿Los OAuth tokens se cifran con Fernet antes de persistir?
- [ ] ¿Los archivos subidos pasan por `file_validation.py` (MIME check)?
- [ ] ¿Las queries SQL usan bind parameters (sin f-strings)?
- [ ] ¿Los logs no contienen datos personales ni tokens?
- [ ] ¿Hay rate limiting en endpoints públicos?

---

## Referencias de Esta Skill

| Recurso | Contenido |
|---------|-----------|
| [`./references/node-implementation.md`](./references/node-implementation.md) | Contrato completo de nodos, AgentState fields, patrones async |
| [`./references/ports-adapters.md`](./references/ports-adapters.md) | Checklist de 8 pasos, Google APIs, MCP server proxy pattern |
| [`./references/hil-patterns.md`](./references/hil-patterns.md) | HIL paso a paso, checkpointer, feedback loop, SSE streaming |
| [`./references/saas-patterns.md`](./references/saas-patterns.md) | Créditos, suscripciones, API keys Enterprise, white-label |
| [`./references/testing-agentic.md`](./references/testing-agentic.md) | Plantillas de tests de nodos, fixtures, mocks obligatorios |
| [`.github/instructions/cashing-agentic-saas.instructions.md`](../instructions/cashing-agentic-saas.instructions.md) | Reglas completas de arquitectura (fuente de verdad) |
| [`context/estado_actual_v1.md`](../../../context/estado_actual_v1.md) | Estado actual, deuda técnica, roadmap de fases |
