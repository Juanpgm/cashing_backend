# Plan de Mejora CashIn — Sistema Agéntico Autónomo de Cuentas de Cobro

> ⚠️ Historical/aspirational — the implemented architecture differs; see CLAUDE.md (custom CompiledGraph engine, no LangGraph/CrewAI).

> **Versión:** 1.0 | **Fecha:** 2026-05-08
> **Actualizar vía:** `/workflow-improver`
> **Estado actual:** Fase 0 — En implementación

---

## Problema

La realización de cuentas de cobro es tediosa y consume tiempo a los contratistas colombianos de prestación de servicios. Cada entidad (pública o privada) exige formatos diferentes, evidencias distintas y documentos adicionales variables, haciendo que el proceso sea difícil de estandarizar y altamente propenso a errores.

## Solución

Sistema agéntico de IA que comprende los requerimientos de cada entidad, recolecta evidencia de cualquier fuente digital (Gmail, Drive, Calendar, PC/USB, cloud), genera toda la documentación necesaria de manera autónoma y organiza el resultado en una estructura de carpetas lista para entregar.

---

## Happy Path

1. Usuario se registra con cédula y datos personales.
2. Sistema detecta contratos activos en SECOP II (o usuario ingresa contrato privado: formulario o PDF).
3. Agente descarga documentos del contrato, extrae obligaciones y perfil de la entidad.
4. Agente identifica plantillas disponibles; solicita las que faltan (HIL ordenado).
5. Agente recolecta evidencia en paralelo desde Gmail, Drive, Calendar y archivos locales.
6. Agente genera el set completo de documentos (cuenta de cobro, informe de actividades, anexos).
7. Documentos organizados en estructura de carpetas `{entidad}/{contrato}/{periodo}/{tipo}`.
8. Usuario aprueba vista previa HTML → sistema genera PDF/DOCX finales.
9. Sistema guarda en S3/Drive; usuario descarga o comparte.

## Sad Path (a evitar)

- Usuario obligado a cargar datos manualmente y en pasos complejos.
- Agente alucina por falta de contexto → documentos incorrectos.
- App lenta o compleja → usuario prefiere hacerlo a mano.

---

## Stack de Decisiones

| Componente | Decisión | Razón |
|------------|----------|-------|
| LLM Routing | `groq/llama-3.1-8b-instant` | < 500ms, ~$0.05/1M tokens |
| LLM Extracción/Generación | `gemini/gemini-2.5-flash` | 1M tokens contexto, español legal, $0.15/1M |
| LLM Judge | `gemini/gemini-2.5-flash` | Suficiente para rubrica estructurada |
| Embeddings | `text-embedding-004` | $0.00002/1K, decente en español |
| LLM Dev/CI | `ollama/qwen2.5:7b` | Gratis, sin red, reproducible |
| Costo por cuenta | $0.03–0.08 USD | Viable a $5–15/mes plan PRO |
| Orquestador | LangGraph | Checkpointing, HIL, persistencia, routing |
| Subagentes complejos | CrewAI (kickoff_async desde nodos LangGraph) | Paralelismo en evidencia/ensamblado |
| Subagentes simples | LangGraph subgraphs | Sin overhead, mismo runtime |
| Frontend | Next.js 15 + Tremor + Assistant-UI + Playwright | MVP casi-producción web |
| Frontend móvil (futuro) | Flutter | Post-Fase 8 |
| SECOP | Solo SECOP II; contratos privados: formulario o PDF | Alcance inicial |
| Multi-tenant | Single-tenant ahora; diseño preparado para futuro | Scope reducido MVP |

---

## Fases del Plan

### Fase 0 — Estabilización y Specs *(fundamento)*

**Estado:** 🟡 En progreso

**Objetivo:** El proyecto compila, los tests pasan, el estado del agente persiste y existe el contrato escrito del sistema.

**Entregables:**
- `docs/AGENT_SPECS.md` — contrato vivo del agente (modos, nodos, presupuestos, criterios de aceptación)
- Migración Alembic `007_agent_runs_borradores` (tablas `agent_runs`, `borradores_cuenta_cobro`)
- `langgraph-checkpoint-postgres` conectado al grafo; `conversacion.id` como `thread_id`
- Modelos SQLAlchemy: `AgentRun`, `BorradorCuentaCobro`
- Cobertura tests ≥ 70% del código existente
- Nuevos `AgentMode` valores en `app/schemas/agent.py`

**Gate de salida:**
- [ ] `pytest` verde con ≥ 70% cobertura
- [ ] `make lint` sin errores
- [ ] Test integración: agente inicia sesión → checkpoint → reinicio → reanuda desde mismo estado
- [ ] `AGENT_SPECS.md` creado y revisado

---

### Fase 1 — Onboarding + Discovery Multi-fuente

**Objetivo:** Usuario se registra, contratos detectados automáticamente en SECOP II o ingresados manualmente, y los adaptadores de ingesta de archivos (local, Drive, Gmail) funcionan desde el día uno.

**Entregables:**
- `secop_discovery_node` + tool `secop_client` (Socrata `jbjy-vk9h` + `p6dx-8zbt`)
- `POST /api/v1/onboarding/secop` (cédula → contratos → descarga documentos)
- `POST /api/v1/onboarding/manual` (formulario campos clave **o** upload PDF — agente extrae igual)
- `local_files_node` + `POST /api/v1/documents/upload-batch` (multipart, cualquier formato)
- Gmail/Drive expuestos como endpoints de "primera carga" en onboarding
- Nuevo modo `AgentMode.SECOP_DISCOVERY`

**Gate de salida:**
- [ ] Unit tests `secop_client` con fixture Socrata mockeada
- [ ] Test integración: cédula real de prueba → contratos encontrados → documentos descargados
- [ ] Test `upload-batch`: 10 archivos mixtos (PDF/DOCX/JPG) → todos persistidos con MIME correcto
- [ ] Smoke E2E: flujo onboarding completo con usuario de prueba

---

### Fase 2 — Plantillas, Requisitos e Identidad de Entidades

**Objetivo:** El agente comprende qué documentos requiere cada entidad, usa la plantilla correcta y pide lo que falta de forma clara.

**Entregables:**
- `requirements_ingestion_node`: guías/correos/PDFs → schema `EntityRequirements`
- `entity_profile_node`: perfil de entidad pagadora (DB, re-uso si existe)
- `template_resolver_node`: busca en `plantillas` → `interrupt()` si falta → usuario sube plantilla (incluye logos)
- `POST /api/v1/templates` con soporte logos + DOCX + PDF
- Detección automática tipo entidad (SENA, Ministerio, ICBF, Universidad, empresa privada)
- Nuevos modos: `REQUIREMENTS_INGESTION`, `TEMPLATE_RESOLVE`

**Gate de salida:**
- [ ] Test: contrato sin plantilla → agente pausa → usuario sube plantilla → reanuda → borrador generado
- [ ] Test: 5 guías de entidades distintas → `EntityRequirements` extraído correctamente
- [ ] Test HIL: `interrupt()` se dispara, endpoint feedback lo reanuda

---

### Fase 3 — Extracción de Obligaciones de Alta Calidad

**Objetivo:** Extracción con ≥ 90% precisión, embeddings para búsqueda semántica, validación automática.

**Entregables:**
- `obligations_extraction_node` mejorado: few-shot por tipo entidad + Pydantic structured output
- pgvector habilitado: migración `008_pgvector_embeddings`
- Embeddings `text-embedding-004` en `obligaciones.embedding`
- `quality_gate_node` (LLM-as-judge)
- Dataset dorado: 10 contratos representativos con obligaciones anotadas manualmente

**Gate de salida:**
- [ ] Evaluación en dataset dorado: precisión ≥ 90%, recall ≥ 85%
- [ ] Test `quality_gate_node`: obligación malformada → rechazada; válida → aprobada
- [ ] Test embeddings: búsqueda semántica → devuelve obligaciones correctas

---

### Fase 4 — Evidence Orchestrator con CrewAI + Subgraphs

**Objetivo:** Barrido paralelo multi-fuente con matching semántico preciso.

**Entregables:**
- `evidence_orchestrator_node` lanza `EvidenceGatheringCrew` (Gmail + Drive + Calendar en paralelo)
- `evidence_matcher_node`: pgvector (coseno) + LLM refinement
- `evidence_dedup_node`: hash + similarity threshold
- LangGraph subgraph para archivos locales ya subidos (sin overhead CrewAI)
- Métricas de evidencia en `agent_runs`

**Gate de salida:**
- [ ] Dataset: 50 emails + 20 Drive + 10 Calendar → recall ≥ 85% sobre 20 obligaciones marcadas
- [ ] Test `evidence_dedup_node`: 5 emails duplicados → 1 por grupo
- [ ] Test `EvidenceGatheringCrew` con mocks → ejecuta en paralelo, consolida resultados

---

### Fase 5 — Ensamblado de Documentos y Organización

**Objetivo:** Set completo de documentos listos para entregar.

**Entregables:**
- `doc_assembly_node` con `DocAssemblyCrew` (cuenta + informe + anexos en paralelo)
- Vista previa HTML: `GET /api/v1/cuentas-cobro/{id}/preview` (obligatoria antes de PDF)
- Endpoint aprobación → genera PDF/DOCX final
- `folder_organizer_node`: estructura `{entidad}/{contrato}/{periodo}/{tipo_doc}` en S3/Drive/local
- Tool `file_organizer` extendida para multi-entidad

**Gate de salida:**
- [ ] Set completo para 5 entidades distintas (con sus plantillas)
- [ ] Checklist manual 20 items: nombre, período, evidencias, logo, formato
- [ ] Test: PDF no se genera sin aprobación de vista previa
- [ ] Test: 3 contratos → estructura de carpetas correcta

---

### Fase 6 — Orquestador Maestro (CUENTA_COBRO_FULL) + HIL Real

**Objetivo:** Un solo comando → cuenta de cobro completa, con pausas naturales en los puntos que requieren input humano.

**Entregables:**
- `supervisor_node`: modo `CUENTA_COBRO_FULL`, razona qué saltarse si ya está hecho
- `human_review_node`: `interrupt()` en puntos clave (falta plantilla, baja confianza, pre-PDF)
- `borradores_cuenta_cobro`: versionado (v1, v2...) + diff entre versiones
- `PATCH /api/v1/agent/sessions/{id}/feedback`
- SSE streaming de estado del agente al frontend
- Agente escala al usuario solo lo que no puede resolver

**Gate de salida:**
- [ ] E2E: "genera mi cuenta de cobro de abril" → flujo completo con HIL simulado → documentos
- [ ] Test borradores: 3 iteraciones de feedback → v3 correcta, historial preservado
- [ ] Test SSE: cliente recibe eventos de progreso de todos los nodos
- [ ] Test timeout HIL: estado guardado, se puede retomar

---

### Fase 7 — Memoria del Agente, MCP propio, Observabilidad

**Objetivo:** El agente aprende del usuario, tiene configuración explícita de herramientas y puede observarse y tunearse.

**Entregables:**
- Tabla `preferencias_usuario` + few-shot con cuentas de cobro aprobadas previas
- `mcp_config.json` versionado: MCPs activos, parámetros, presupuesto de tokens por herramienta
- `mcp_filesystem_server.py`: indexa PC/USB del usuario en sesiones desktop
- Langfuse self-hosted en Railway: trazas de todos los LLM calls, scores, latencias
- `GET /api/v1/admin/agent-runs` con métricas por usuario/nodo/modelo

**Gate de salida:**
- [ ] Test memoria: cuenta aprobada → siguiente usa mismo tono/formato sin instrucciones
- [ ] Trazas Langfuse visibles con latencia, tokens, score de judge
- [ ] Test MCP filesystem: carpeta local indexada → archivos encontrados en evidence search
- [ ] Test `mcp_config.json`: cambio de temperatura desde config → agente usa nueva temperatura

---

### Fase 8 — Frontend MVP (casi-producción)

**Stack:** Next.js 15 + React 19 + Tailwind + shadcn/ui + Tremor + Assistant-UI + LangGraph JS SDK + TanStack Query + Playwright

**Objetivo:** Interfaz de usuario completa para admin/dev/pruebas y también para el contratista final (MVP casi-producción).

**Entregables:**
- Onboarding wizard: cédula → SECOP → contratos → Google OAuth → plantillas
- Chat agéntico: streaming SSE, visualización nodos activos, botones HIL
- Dashboard Tremor: runs, costo/sesión, calidad extracción, tasa éxito por entidad
- Panel tuning: ver/editar prompts por nodo, ajustar temperatura, ver trazas Langfuse
- Panel plantillas: gestionar templates por entidad, subir logos
- Upload batch: drag & drop de archivos/carpetas
- Vista previa y aprobación de documentos
- Descarga/compartir documentos finales

**Gate de salida:**
- [ ] Playwright E2E: happy path completo (registro → SECOP → plantilla → cuenta → descarga)
- [ ] Playwright: flujo HIL (agente pausa → usuario responde → agente continúa)
- [ ] Playwright: upload batch (drag & drop)
- [ ] QA humano: checklist 20 items por flujo principal

---

## Nodos del Grafo — Mapa Completo

### Modos nuevos añadidos al grafo

```
[User Input] → [Router]
  ├── CUENTA_COBRO_FULL → [Supervisor] ─────────────────────────────────────┐
  │       ├──→ [SECOP Discovery] → [Requirements Ingestion] → [Entity Profile]
  │       ├──→ [Template Resolver] ──HIL si falta──→ continúa              │
  │       ├──→ [Obligations Extraction] → [Quality Gate]                   │
  │       ├──→ [Evidence Orchestrator] ─→ CrewAI(Gmail+Drive+Cal+Local)    │
  │       │         └──→ [Evidence Matcher] → [Evidence Dedup]             │
  │       ├──→ [Doc Assembly] ─→ CrewAI(Cuenta+Informe+Anexos)             │
  │       ├──→ [Folder Organizer]                                           │
  │       └──→ [Human Review HIL] ──aprueba──→ PDF final ──────────────────┘
  ├── SECOP_DISCOVERY → [SECOP Discovery] → END
  ├── REQUIREMENTS_INGESTION → [Requirements Ingestion] → END
  ├── TEMPLATE_RESOLVE → [Template Resolver] → END
  ├── QUALITY_GATE → [Quality Gate] → END
  └── (modos existentes sin cambio: CHAT, PIPELINE, EVIDENCE, DRIVE, EXTRACT_OBLIGATIONS, GENERATE_ACTIVITIES, CONFIG)
```

### Tabla de nodos nuevos

| Nodo | Archivo | Modo(s) | Subagente |
|------|---------|---------|-----------|
| `supervisor_node` | `nodes/supervisor.py` | `CUENTA_COBRO_FULL` | — |
| `secop_discovery_node` | `nodes/secop_discovery.py` | `SECOP_DISCOVERY`, `CUENTA_COBRO_FULL` | — |
| `requirements_ingestion_node` | `nodes/requirements_ingestion.py` | `REQUIREMENTS_INGESTION`, `CUENTA_COBRO_FULL` | — |
| `entity_profile_node` | `nodes/entity_profile.py` | `CUENTA_COBRO_FULL` | — |
| `template_resolver_node` | `nodes/template_resolver.py` | `TEMPLATE_RESOLVE`, `CUENTA_COBRO_FULL` | — |
| `evidence_orchestrator_node` | `nodes/evidence_orchestrator.py` | `CUENTA_COBRO_FULL` | `EvidenceGatheringCrew` |
| `local_files_node` | `nodes/local_files.py` | `CUENTA_COBRO_FULL` | subgraph |
| `evidence_matcher_node` | `nodes/evidence_matcher.py` | `CUENTA_COBRO_FULL` | — |
| `evidence_dedup_node` | `nodes/evidence_dedup.py` | `CUENTA_COBRO_FULL` | — |
| `doc_assembly_node` | `nodes/doc_assembly.py` | `CUENTA_COBRO_FULL` | `DocAssemblyCrew` |
| `folder_organizer_node` | `nodes/folder_organizer.py` | `CUENTA_COBRO_FULL` | — |
| `quality_gate_node` | `nodes/quality_gate.py` | `QUALITY_GATE`, `CUENTA_COBRO_FULL` | — |
| `human_review_node` | `nodes/human_review.py` | `CUENTA_COBRO_FULL` | — |

---

## Tools / Skills Nuevas

| Tool/Skill | Archivo | Propósito |
|------------|---------|-----------|
| `secop_client` | `tools/secop_client.py` | Cliente Socrata SECOP II |
| `requirements_parser` | `tools/requirements_parser.py` | LLM + regex → `EntityRequirements` |
| `template_registry` | `tools/template_registry.py` | CRUD `plantillas` + detección placeholders |
| `local_storage_adapter` | `adapters/local/` | Drop-folder + USB indexing |
| `vector_search` | `tools/vector_search.py` | pgvector cosine + `text-embedding-004` |
| `judge_skill` | `tools/judge.py` | LLM-as-judge con rubrica |
| `mcp_filesystem_server` | `mcp_servers/filesystem_server.py` | MCP para PC/USB del usuario |
| `EvidenceGatheringCrew` | `agent/crews/evidence_crew.py` | CrewAI: Gmail+Drive+Calendar paralelo |
| `DocAssemblyCrew` | `agent/crews/doc_assembly_crew.py` | CrewAI: Cuenta+Informe+Anexos paralelo |

---

## Estrategia de Tests (transversal)

| Capa | Herramienta | Cuándo |
|------|-------------|--------|
| Unit (nodos, tools) | pytest + mocks LLM | Cada nodo/tool nuevo |
| Integración (DB + adapters) | pytest-asyncio + testcontainers | Cada fase |
| Agente E2E (grafo completo con LLM mock) | LangGraph test harness | Cada modo nuevo |
| LLM eval (calidad real) | Dataset dorado + judge | Fases 3, 4, 5 |
| Contract API | schemathesis sobre OpenAPI | Antes de Fase 8 |
| E2E UI | Playwright | Fase 8 |
| Carga | Locust (50 usuarios concurrentes) | Pre-producción |

---

## Dependencias Nuevas

```
# requirements.txt
langgraph-checkpoint-postgres>=2.0
crewai>=0.100
crewai-tools>=0.40
pgvector>=0.3
langfuse>=3.0

# requirements-dev.txt
testcontainers[postgres]>=4.0
playwright>=1.44
locust>=2.30
```

---

## Archivos de Configuración del Agente

| Archivo | Propósito | Actualizable vía |
|---------|-----------|-----------------|
| `docs/AGENT_SPECS.md` | Contrato vivo del agente | `/workflow-improver` |
| `mcp_servers/mcp_config.json` | MCPs activos, parámetros | UI panel tuning / manual |
| `.github/instructions/cashing-agentic-saas.instructions.md` | Filosofía rectora | `/workflow-improver` |
| `app/agent/prompts/` | Prompts por nodo | Panel tuning Fase 8 |

---

## Historial de Cambios

| Versión | Fecha | Cambio |
|---------|-------|--------|
| 1.0 | 2026-05-08 | Plan inicial creado |
