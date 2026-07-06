# Implementación de Nodos LangGraph — Referencia Completa

## Contrato de un Nodo

```python
# app/agent/nodes/{modo}.py
from app.agent.state import AgentState
from app.core.exceptions import AgentError
import structlog

logger = structlog.get_logger("agent.nodes.{modo}")

async def mi_nodo(state: AgentState) -> dict:
    """
    Nodo del grafo LangGraph.
    Recibe el estado completo, retorna solo los campos que modifica.
    """
    session_id = state["session_id"]
    user_id = state["user_id"]
    db = state["_db"]  # AsyncSession — inyectado por AgentService

    start = time.monotonic()
    try:
        # ... lógica del nodo ...
        resultado = await _procesar(state)
        
        logger.info(
            "mi_nodo.completed",
            session_id=str(session_id),
            user_id=str(user_id),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return {"campo_resultado": resultado}  # Solo campos modificados

    except SomeExternalError as e:
        logger.error("mi_nodo.failed", error=str(e), session_id=str(session_id))
        raise AgentError(f"mi_nodo falló: {e}") from e
```

## Campos de AgentState por Categoría

### Campos de Sesión (siempre presentes)
```python
session_id: uuid.UUID        # ID único de la sesión
user_id: uuid.UUID           # Usuario autenticado
mode: AgentMode              # Modo actual del agente
```

### Campos de Conversación
```python
messages: list[dict]         # Historial de mensajes (LangChain format)
user_input: str | None       # Último input del usuario
response: str | None         # Respuesta actual del agente
```

### Campos PIPELINE (procesamiento de contratos)
```python
document_id: uuid.UUID | None
document_text: str | None        # Texto extraído del PDF/DOCX
document_metadata: dict | None   # Metadatos del archivo
extracted_data: dict | None      # Datos estructurados extraídos
classification: str | None       # Tipo de documento clasificado
justification: str | None        # Justificación narrativa generada
```

### Campos EVIDENCE (recolección de evidencia)
```python
contrato_contexto: dict | None          # Datos del contrato activo
obligaciones_contexto: list[dict] | None # Obligaciones del período
email_query: str | None                 # Query construida para Gmail
email_evidence: list[dict] | None       # Emails encontrados
email_message_ids: list[str] | None     # IDs para trazabilidad
email_sent: bool | None
email_sent_id: str | None
```

### Campos EXTRACT_OBLIGATIONS
```python
texto_contrato: str | None         # Texto completo del contrato
contrato_id_str: str | None        # UUID del contrato en BD
document_bytes: bytes | None       # ← usar _document_bytes (no checkpointeable)
document_filename: str | None
contrato_extraido: dict | None     # Metadatos extraídos del contrato
obligaciones_extraidas: list[dict] | None  # Obligaciones extraídas
```

### Campos GENERATE_ACTIVITIES
```python
cuenta_cobro_id_str: str | None    # UUID de la cuenta de cobro
mes: int | None                    # Mes del período (1-12)
anio: int | None                   # Año del período
actividades_generadas: list[dict] | None  # Lista de actividades
```

### Campos HIL (Human-in-the-Loop)
```python
awaiting_human_approval: bool | None
human_approval_type: str | None   # "justification" | "activities" | "upload"
user_feedback: str | None
```

### Campos de Runtime (NO checkpointeables — prefijo _)
```python
_db: Any           # AsyncSession — NUNCA serializar
_pdf_bytes: Any    # bytes — demasiado pesado para checkpoint
_pdf_filename: Any
```

## Agregar Campos Nuevos

1. Agregar en `AgentState` TypedDict con tipo explícito y `| None`
2. Si es no serializable (bytes, objetos externos): usar prefijo `_`
3. Documentar en comentario inline qué nodo lo produce y qué nodo lo consume

## Temperaturas y Modelos de Referencia

| Tipo de tarea | Modelo | Temperatura |
|--------------|--------|-------------|
| Routing / clasificación (determinista) | `groq/llama-3.1-8b-instant` | `0.0` |
| Extracción estructurada (JSON preciso) | `gemini/gemini-2.5-flash` | `0.1` |
| Matching / comparación semántica | `gemini/gemini-2.5-flash` | `0.0` |
| Narrativa / justificación | `gemini/gemini-2.5-flash` | `0.3` |
| Chat conversacional | `gemini/gemini-2.5-flash` | `0.4` |
| Dev / testing local | `ollama/qwen2.5:7b` | `0.1` |

## Estructura de Prompts

```python
# app/agent/prompts/{modo}.py
SYSTEM_PROMPT = """
Eres un asistente experto en [dominio específico].
Contexto del sistema: CashIn — plataforma para contratistas colombianos.
Responde SIEMPRE en español formal colombiano.
"""

USER_PROMPT_TEMPLATE = """
{campo_del_estado_1}
{campo_del_estado_2}

Instrucción: [tarea concreta]
Formato de respuesta: [JSON schema o texto]
"""
```

## Anti-patrones a Evitar

```python
# ❌ Retornar el estado completo
async def mi_nodo(state: AgentState) -> AgentState:
    state["response"] = "algo"
    return state  # INCORRECTO — modifica en-place y retorna todo

# ✅ Solo retornar campos modificados
async def mi_nodo(state: AgentState) -> dict:
    return {"response": "algo"}

# ❌ Importar la BD directamente
from app.core.database import get_db  # NO en nodos

# ✅ Usar la sesión inyectada
db = state["_db"]

# ❌ Guardar bytes en campo checkpointeable
document_bytes: bytes  # En checkpoint → falla con objetos grandes

# ✅ Usar prefijo _ para no-serializables
_document_bytes: Any   # Prefijo _ → no incluido en checkpoint
```
