# HIL (Human-in-the-Loop) — Referencia Completa

## Qué es HIL en CashIn

El agente puede **pausar su ejecución** en puntos donde el usuario debe revisar o aprobar antes de continuar. Esto es crítico para la cuenta de cobro final: el usuario aprueba el borrador antes de generar el PDF y enviarlo.

## Puntos de Pausa Actuales

| Punto de pausa | Nodo previo | Qué espera | Endpoint de reanudación |
|---------------|------------|-----------|------------------------|
| Borrador de justificación | `justification_node` | Revisar texto | `PATCH /api/v1/agent/sessions/{id}/feedback` |
| Lista de actividades | `generate_activities_node` | Aprobar/editar | `PATCH /api/v1/agent/sessions/{id}/approve-activities` |
| Antes de subir a Drive | `doc_generation_node` | Confirmar subida | `POST /api/v1/agent/sessions/{id}/confirm-upload` |

## Flujo Completo de HIL

```
Nodo produce borrador
        │
        ▼
Retorna {awaiting_human_approval: True, ...}
        │
        ▼
AgentService detecta flag → persiste estado en borradores_cuenta_cobro
        │
        ▼
SSE envía evento "awaiting_review" al frontend
        │
    Usuario revisa en UI
        │
        ▼
POST /api/v1/agent/sessions/{id}/feedback  { feedback: "..." }
        │
        ▼
AgentService carga estado → inyecta user_feedback → reanuda grafo
        │
        ▼
apply_feedback_node refina el borrador con LLM
        │
        ▼
Nodo retorna {awaiting_human_approval: False, justification: "versión refinada"}
```

## Implementar un Nuevo Punto de Pausa

### 1. Agregar campos al AgentState

```python
# app/agent/state.py
awaiting_human_approval: bool | None
human_approval_type: str | None   # "justification" | "activities" | "upload" | tu nuevo tipo
user_feedback: str | None
borrador_version: int | None      # Para versioning de borradores
```

### 2. Nodo que produce el borrador

```python
# app/agent/nodes/mi_nodo.py
async def mi_nodo(state: AgentState) -> dict:
    borrador = await llm_call(SYSTEM_PROMPT, contexto)
    
    return {
        "mi_campo_borrador": borrador,
        "awaiting_human_approval": True,
        "human_approval_type": "mi_tipo",
        "borrador_version": 1,
    }
```

### 3. Nodo de aplicar feedback

```python
# app/agent/nodes/mi_nodo.py
async def apply_mi_feedback_node(state: AgentState) -> dict:
    borrador_original = state["mi_campo_borrador"]
    feedback = state.get("user_feedback", "")
    
    borrador_refinado = await llm_call(
        REFINE_PROMPT,
        original=borrador_original,
        feedback=feedback
    )
    
    nueva_version = (state.get("borrador_version") or 1) + 1
    
    return {
        "mi_campo_borrador": borrador_refinado,
        "awaiting_human_approval": False,
        "user_feedback": None,    # Limpiar para próxima iteración
        "borrador_version": nueva_version,
    }
```

### 4. Edge condicional en el grafo

```python
# app/agent/graph.py
def _route_after_mi_nodo(state: AgentState) -> str:
    if state.get("awaiting_human_approval"):
        return "__end__"    # Pausar el grafo
    return "siguiente_nodo"

builder.add_conditional_edges("mi_nodo", _route_after_mi_nodo, {
    "__end__": END,
    "siguiente_nodo": "siguiente_nodo"
})
```

### 5. Endpoint de feedback

```python
# app/api/v1/agent.py
@router.patch("/sessions/{session_id}/feedback")
async def submit_feedback(
    session_id: UUID,
    body: FeedbackRequest,   # { feedback: str }
    current_user: Usuario = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await agent_service.get_session(db, session_id, current_user.id)
    if session.estado != "awaiting_review":
        raise HTTPException(400, "La sesión no está esperando revisión")
    
    # Inyectar feedback y reanudar el grafo
    result = await agent_service.resume_with_feedback(
        db=db,
        session_id=session_id,
        feedback=body.feedback,
        user_id=current_user.id,
    )
    return result
```

## Tabla BD: borradores_cuenta_cobro

```sql
CREATE TABLE borradores_cuenta_cobro (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cuenta_cobro_id UUID REFERENCES cuentas_cobro(id) ON DELETE CASCADE,
    version INTEGER DEFAULT 1,
    contenido TEXT NOT NULL,           -- Markdown o HTML del borrador
    feedback_usuario TEXT,             -- Feedback recibido del usuario
    aprobado BOOLEAN DEFAULT FALSE,    -- True cuando el usuario aprueba
    tipo TEXT DEFAULT 'justificacion', -- 'justificacion' | 'actividades' | 'documento'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Índice para buscar borradores pendientes por cuenta
CREATE INDEX ON borradores_cuenta_cobro(cuenta_cobro_id, aprobado)
WHERE aprobado = FALSE;
```

## Streaming SSE — Notificar al Frontend

Cuando el agente pausa, enviar evento SSE:

```python
# En AgentService, al detectar awaiting_human_approval
async def _stream_hil_event(self, session_id: UUID, tipo: str, borrador: str):
    event = {
        "type": "awaiting_review",
        "session_id": str(session_id),
        "approval_type": tipo,
        "draft_content": borrador,
        "timestamp": datetime.utcnow().isoformat(),
    }
    yield f"data: {json.dumps(event)}\n\n"
```

## LangGraph Checkpointer (Prerequisito para HIL Robusto)

Para HIL en producción real (no solo en memoria), instalar:

```bash
uv add langgraph-checkpoint-postgres
```

```python
# app/agent/graph.py
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

async def build_graph_with_checkpointer(db_url: str):
    checkpointer = AsyncPostgresSaver.from_conn_string(db_url)
    await checkpointer.setup()  # Crea tablas de checkpoint
    
    graph = build_graph()
    return graph.compile(checkpointer=checkpointer)
```

```python
# Al invocar el grafo — thread_id = session_id del usuario
config = {"configurable": {"thread_id": str(session_id)}}
result = await graph.ainvoke(state, config=config)

# Al reanudar (después del feedback del usuario)
result = await graph.ainvoke(
    {"user_feedback": feedback},
    config=config   # Mismo thread_id → reanuda desde el checkpoint
)
```
