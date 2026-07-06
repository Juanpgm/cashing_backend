# AGENT_SPECS — Contrato Vivo del Agente CashIn

> **Versión:** 1.0 | **Fecha:** 2026-05-08
> **Actualizar vía:** `/workflow-improver`
> **Fuente de verdad** para el comportamiento esperado del agente LangGraph.
> Si hay conflicto entre código y este documento, el código es la implementación actual y este documento es el objetivo.

---

## 1. Identidad del Agente

- **Nombre:** CashIn Agent
- **Objetivo:** Automatizar el ciclo completo de generación de cuentas de cobro para contratistas colombianos.
- **Paradigma:** Agent-First. Los endpoints FastAPI son puertas de acceso al agente, no la lógica en sí.
- **Modelo de ejecución:** LangGraph stateful graph con checkpointing PostgreSQL. Subagentes CrewAI para tareas paralelas complejas.

---

## 2. Modos de Operación

| Modo | `AgentMode` valor | Descripción | Estado |
|------|------------------|-------------|--------|
| Chat conversacional | `CHAT` | Preguntas y ayuda contextual | ✅ Implementado |
| Pipeline de documento | `PIPELINE` | Procesar PDF/DOCX subido por usuario | ✅ Implementado |
| Recolección evidencia | `EVIDENCE` | Buscar emails Gmail para obligaciones | ✅ Implementado |
| Subida Drive | `DRIVE` | Subir documento a Google Drive | ✅ Implementado |
| Extracción obligaciones | `EXTRACT_OBLIGATIONS` | Extraer obligaciones del contrato | ✅ Implementado |
| Generación actividades | `GENERATE_ACTIVITIES` | Generar lista de actividades | ✅ Implementado |
| Configuración | `CONFIG` | Configuración conversacional | ✅ Implementado (→ chat) |
| Discovery SECOP | `SECOP_DISCOVERY` | Detectar contratos por cédula en SECOP II | 🔲 Fase 1 |
| Ingesta requisitos | `REQUIREMENTS_INGESTION` | Parsear guías/correos de entidad → EntityRequirements | 🔲 Fase 2 |
| Resolución plantilla | `TEMPLATE_RESOLVE` | Resolver/solicitar plantilla para entidad | 🔲 Fase 2 |
| Quality gate | `QUALITY_GATE` | Validar calidad de obligaciones/evidencias extraídas | 🔲 Fase 3 |
| **Cuenta cobro completa** | `CUENTA_COBRO_FULL` | **Orquestador maestro: flujo end-to-end** | 🔲 Fase 6 |

---

## 3. Nodos del Grafo — Especificaciones

### 3.1 Nodos Existentes

#### `router_node`
- **Modelo:** `groq/llama-3.1-8b-instant`
- **Temperatura:** 0.0 (determinista)
- **Presupuesto tokens:** 500 input + 50 output
- **Output:** `AgentMode` en `state.mode`
- **Criterio de aceptación:** Clasifica correctamente ≥ 95% de intenciones en test set de 50 prompts

#### `chat_node`
- **Modelo:** `gemini/gemini-2.5-flash`
- **Temperatura:** 0.4
- **Presupuesto tokens:** 8,000 input + 2,000 output
- **Output:** `state.response` (string)
- **Criterio de aceptación:** Responde en español colombiano, sin alucinaciones sobre datos del usuario

#### `doc_ingestion_node`
- **Herramientas:** `document_parser`, `pdfplumber`, `pytesseract` (OCR fallback)
- **Output:** `state.document_text`, `state.document_metadata`
- **Criterio de aceptación:** Extrae texto de PDF/DOCX/JPG correctamente; error explícito si falla OCR

#### `doc_understanding_node`
- **Modelo:** `gemini/gemini-2.5-flash`
- **Temperatura:** 0.1
- **Presupuesto tokens:** 50,000 input + 3,000 output
- **Output:** `state.extracted_data`
- **Criterio de aceptación:** Extrae campos clave (partes, objeto, valor, vigencia) con ≥ 90% precisión

#### `obligations_extraction_node`
- **Modelo:** `gemini/gemini-2.5-flash`
- **Temperatura:** 0.1
- **Presupuesto tokens:** 100,000 input + 8,000 output (contexto largo contrato)
- **Output:** `state.obligaciones_extraidas` (lista Pydantic estructurada)
- **Criterio de aceptación:** Precisión ≥ 90%, recall ≥ 85% (dataset dorado 10 contratos — Fase 3)
- **Few-shot:** Sí, por tipo de entidad (SENA, Ministerio, Universidad, empresa privada)

#### `email_fetch_node`
- **Herramientas:** MCP `gmail_server`
- **Temperatura:** 0.0
- **Output:** `state.email_evidence` (lista de dicts)
- **Criterio de aceptación:** Recall ≥ 85% sobre emails relevantes para obligaciones

#### `generate_activities_node`
- **Modelo:** `gemini/gemini-2.5-flash`
- **Temperatura:** 0.3
- **Output:** `state.actividades_generadas`
- **Criterio de aceptación:** Actividades coherentes con obligaciones; formato de fecha correcto

### 3.2 Nodos Nuevos (a implementar por fase)

#### `secop_discovery_node` *(Fase 1)*
- **Tool:** `secop_client` (Socrata API `jbjy-vk9h` + `p6dx-8zbt`)
- **Input:** `state.cedula` (str)
- **Output:** `state.secop_contratos` (lista), `state.secop_documentos` (lista)
- **HIL trigger:** Si ningún contrato encontrado → solicitar confirmación o entrada manual
- **Criterio de aceptación:** Encuentra contratos con cédula real de prueba; falla limpia si SECOP no responde

#### `requirements_ingestion_node` *(Fase 2)*
- **Modelo:** `gemini/gemini-2.5-flash`
- **Temperatura:** 0.1
- **Input:** Archivo guía (PDF/DOCX/TXT/email) en `state.document_bytes`
- **Output:** `state.entity_requirements` (`EntityRequirements` Pydantic schema)
- **Criterio de aceptación:** 5 guías de entidades distintas → schema extraído correctamente (validado manual)

#### `entity_profile_node` *(Fase 2)*
- **Input:** `state.entity_requirements`, `state.contrato_extraido`
- **Output:** `state.entity_profile_id` (UUID del perfil en DB)
- **Criterio de aceptación:** Re-usa perfil existente si la entidad ya fue procesada antes

#### `template_resolver_node` *(Fase 2)*
- **Input:** `state.entity_profile_id`, `state.document_type` (tipo doc a generar)
- **Output:** `state.template_id` (UUID) o `interrupt()` si falta
- **HIL trigger:** Si no hay plantilla → `interrupt()` con mensaje claro: qué plantilla se necesita, qué campos debe tener, ejemplo de nombre
- **Criterio de aceptación:** Pausa correctamente; reanuda tras upload sin perder estado

#### `quality_gate_node` *(Fase 3)*
- **Modelo:** `gemini/gemini-2.5-flash` (como judge)
- **Temperatura:** 0.0
- **Input:** `state.obligaciones_extraidas`
- **Output:** `state.quality_gate_passed` (bool), `state.quality_issues` (lista)
- **Rubrica:** Completitud, coherencia, duplicados, formato de fecha, referencia a cláusula
- **Criterio de aceptación:** Obligación malformada → rechazada; obligación válida → aprobada

#### `evidence_orchestrator_node` *(Fase 4)*
- **Subagente:** `EvidenceGatheringCrew` (CrewAI, `kickoff_async`)
- **Agentes internos:** `GmailSearchAgent`, `DriveSearchAgent`, `CalendarSearchAgent`
- **Input:** `state.obligaciones_extraidas`, `state.contrato_contexto`, período (mes/año)
- **Output:** `state.evidence_raw` (lista consolidada, multi-fuente)
- **Timeout:** 120 segundos por crew
- **Criterio de aceptación:** Recall ≥ 85% sobre dataset benchmark (Fase 4)

#### `local_files_node` *(Fase 4)*
- **Subgraph:** LangGraph subgraph simple (sin CrewAI)
- **Input:** `state.uploaded_file_ids` (archivos ya en S3 vía upload-batch)
- **Output:** `state.local_evidence` (lista procesada)
- **Criterio de aceptación:** Procesa todos los formatos soportados sin error

#### `evidence_matcher_node` *(Fase 4)*
- **Estrategia:** pgvector cosine similarity (umbral ≥ 0.75) → LLM refinement (top-5 candidatos)
- **Modelo LLM:** `groq/llama-3.1-8b-instant` (clasificación binaria relevante/no)
- **Output:** `state.matched_evidence` (obligación → lista de evidencias rankeadas)
- **Criterio de aceptación:** Evidencia correcta rankeada #1 para cada obligación del dataset

#### `evidence_dedup_node` *(Fase 4)*
- **Estrategia:** SHA-256 del contenido + cosine similarity ≥ 0.95
- **Output:** `state.deduplicated_evidence`
- **Criterio de aceptación:** 5 emails duplicados → queda 1 por grupo

#### `doc_assembly_node` *(Fase 5)*
- **Subagente:** `DocAssemblyCrew` (CrewAI, agentes: `CuentaCobroAgent`, `InformeActividadesAgent`, `AnexosAgent`)
- **Input:** Todos los campos del estado (obligaciones, evidencia, perfil entidad, plantilla)
- **Output:** `state.document_drafts` (lista de borradores por tipo documento)
- **Prerrequisito:** `state.template_id` debe existir
- **Criterio de aceptación:** Set completo para 5 entidades distintas pasa checklist de 20 items

#### `folder_organizer_node` *(Fase 5)*
- **Estructura:** `{entidad_slug}/{referencia_contrato}/{YYYY-MM}/{tipo_doc}/`
- **Backends:** S3 + Google Drive + descarga local
- **Output:** `state.folder_manifest` (paths de cada archivo)
- **Criterio de aceptación:** 3 contratos → estructura correcta y consistente

#### `supervisor_node` *(Fase 6)*
- **Modelo:** `gemini/gemini-2.5-flash`
- **Temperatura:** 0.0
- **Rol:** Decide qué nodos ejecutar, cuáles saltarse (ya completados), cuándo hacer HIL
- **Input:** Estado completo de la sesión
- **Output:** Decisión del siguiente nodo + justificación
- **Criterio de aceptación:** "Genera mi cuenta de cobro de abril" → flujo completo correcto en ≤ 5 min

#### `human_review_node` *(Fase 6)*
- **Mecanismo:** LangGraph `interrupt()`
- **Puntos de pausa:**
  1. Falta plantilla (escala desde `template_resolver_node`)
  2. Confianza de extracción < 0.7
  3. Antes de generar PDF final
- **Mensaje HIL:** Claro, ordenado, con opciones concretas para el usuario
- **Timeout:** 24 horas → estado guardado, sesión reanudable
- **Criterio de aceptación:** Pausa real, estado persiste, reanuda sin perder datos

---

## 4. LLM Budget Global (por sesión `CUENTA_COBRO_FULL`)

| Etapa | Modelo | Tokens estimados | Costo estimado |
|-------|--------|-----------------|----------------|
| Routing | groq/llama-3.1-8b-instant | 2K | < $0.001 |
| Extracción contrato | gemini/gemini-2.5-flash | 80K | ~$0.012 |
| Extracción obligaciones | gemini/gemini-2.5-flash | 100K | ~$0.015 |
| Quality gate | gemini/gemini-2.5-flash | 10K | ~$0.0015 |
| Evidence matching | groq/llama-3.1-8b-instant | 20K | ~$0.001 |
| Ensamblado documentos | gemini/gemini-2.5-flash | 50K | ~$0.0075 |
| **TOTAL** | — | ~262K | **~$0.037** |

**Límite de créditos por operación `CUENTA_COBRO_FULL`:** 5 créditos (configurable).

---

## 5. Estado del Agente (`AgentState`) — Campos por Fase

### Fase 0 (actuales + nuevos campos de tracking)
```python
# Tracking de runs
agent_run_id: uuid.UUID | None
current_phase: str | None          # "secop_discovery", "evidence", etc.
quality_scores: dict[str, float] | None
```

### Fase 1 (SECOP + onboarding)
```python
cedula: str | None
secop_contratos: list[dict] | None
secop_documentos: list[dict] | None
uploaded_file_ids: list[uuid.UUID] | None
onboarding_mode: Literal["secop", "manual"] | None
```

### Fase 2 (plantillas + entidad)
```python
entity_requirements: dict | None    # EntityRequirements serializado
entity_profile_id: uuid.UUID | None
template_id: uuid.UUID | None
document_type: str | None           # "cuenta_cobro", "informe_actividades", "anexo"
hil_reason: str | None              # Motivo de la pausa HIL
```

### Fase 3 (calidad)
```python
quality_gate_passed: bool | None
quality_issues: list[str] | None
```

### Fase 4 (evidencia)
```python
evidence_raw: list[dict] | None
local_evidence: list[dict] | None
matched_evidence: dict[str, list[dict]] | None   # obligacion_id → [evidencias]
deduplicated_evidence: list[dict] | None
```

### Fase 5 (documentos)
```python
document_drafts: list[dict] | None
preview_html: str | None
preview_approved: bool | None
folder_manifest: dict[str, str] | None   # tipo_doc → path/url
```

### Fase 6 (supervisor + HIL)
```python
supervisor_plan: list[str] | None        # nodos planificados en orden
borrador_version: int | None
human_review_pending: bool | None
```

---

## 6. Criterios de Calidad por Fase (Gates)

| Fase | Métrica | Umbral mínimo | Herramienta de medición |
|------|---------|---------------|------------------------|
| 0 | Cobertura tests | ≥ 70% | pytest-cov |
| 1 | SECOP unit tests | 100% green | pytest |
| 2 | HIL pausa+reanuda | Funcional | pytest (LangGraph interrupt) |
| 3 | Precisión obligaciones | ≥ 90% | Dataset dorado manual |
| 3 | Recall obligaciones | ≥ 85% | Dataset dorado manual |
| 4 | Recall evidencia | ≥ 85% | Dataset benchmark |
| 5 | Checklist documentos | ≥ 18/20 items | QA humano |
| 6 | E2E cuenta completa | Flujo sin errores | pytest E2E |
| 7 | Memoria preserva tono | Funcional | Test automatizado |
| 8 | Playwright E2E | 100% green | Playwright CI |

---

## 7. Reglas de Comportamiento del Agente

1. **Nunca alucinares datos del contrato** — Si no encuentra información, pide al usuario, no inventa.
2. **Mínimo esfuerzo del usuario** — Siempre intentar resolver antes de escalar al humano.
3. **HIL claro y ordenado** — Cuando escale al usuario: qué necesita, por qué, cómo proporcionarlo, ejemplo.
4. **Fallback graceful** — Si un modelo falla → siguiente en cadena → informar al usuario si todos fallan.
5. **Tokens eficientes** — Usar modelo más barato que cumpla la tarea; reservar Gemini Flash para extracción larga.
6. **Sin hardcoding de entidades** — Toda lógica de entidad pasa por `entity_profile_node` y DB.
7. **Idempotencia** — Re-ejecutar el mismo nodo con el mismo input produce el mismo output.
8. **Transparencia** — Streaming SSE siempre activo para el frontend; usuario sabe en qué nodo está el agente.

---

## 8. Configuración MCP (`mcp_config.json`)

```json
{
  "version": "1.0",
  "servers": {
    "gmail": {
      "command": "uv",
      "args": ["run", "python", "mcp_servers/gmail_server.py"],
      "enabled": true,
      "token_budget": 10000
    },
    "drive": {
      "command": "uv",
      "args": ["run", "python", "mcp_servers/drive_server.py"],
      "enabled": true,
      "token_budget": 5000
    },
    "calendar": {
      "command": "uv",
      "args": ["run", "python", "mcp_servers/calendar_server.py"],
      "enabled": true,
      "token_budget": 3000
    },
    "filesystem": {
      "command": "uv",
      "args": ["run", "python", "mcp_servers/filesystem_server.py"],
      "enabled": false,
      "token_budget": 5000,
      "note": "Activar en Fase 7 — requiere mcp_filesystem_server.py"
    }
  },
  "global_token_budget_per_session": 300000,
  "fallback_chain": [
    "gemini/gemini-2.5-flash",
    "groq/llama-3.3-70b-versatile",
    "ollama/qwen2.5:7b"
  ]
}
```

---

## 9. Historial de Cambios

| Versión | Fecha | Cambio |
|---------|-------|--------|
| 1.0 | 2026-05-08 | Especificación inicial — Fase 0 |
