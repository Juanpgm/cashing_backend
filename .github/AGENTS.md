# CashIn Backend — Playbooks por Tipo de Tarea (AGENTS)

> Cada agente define un playbook táctico: objetivo, entradas, reglas y criterios de salida.
> **MVP en Railway** — sin dependencia de cloud providers.
> Arquitectura Ports & Adapters permite migración futura transparente.

---

## Prioridad de Contexto

1. `INSTRUCTIONS.md` — Arquitectura, reglas, roadmap
2. `SKILLS.md` — Competencias técnicas
3. `TOOLS.md` — Stack de desarrollo y deploy
4. `AGENTS.md` — Playbooks tácticos (este archivo)

Si hay conflicto, prevalece el orden anterior.

---

## Agente: API Contracts

### Objetivo

Diseñar y mantener endpoints FastAPI consistentes, tipados y versionados bajo `/api/v1`.

### Entradas esperadas

- Caso de uso funcional
- Rol/autorización requerida
- Payload de request/response esperado

### Reglas de ejecución

1. Definir/actualizar schemas Pydantic v2 en `app/schemas/` primero
2. Implementar endpoint en `app/api/v1/`
3. Delegar toda lógica al service correspondiente — **nunca SQL en routers**
4. Inyectar dependencias via `app/api/deps.py` (`get_db`, `get_current_user`, `get_storage`, `get_llm`)
5. Usar códigos HTTP correctos: `200`, `201`, `202`, `204`, `400`, `401`, `403`, `404`, `409`, `422`
6. Rate limiting via `slowapi` en endpoints públicos

### Criterios de salida

- Endpoint documentado automáticamente en OpenAPI (`/docs`)
- Sin lógica de negocio ni SQL en router
- Tests de integración con `httpx.AsyncClient`

---

## Agente: Domain Services

### Objetivo

Implementar reglas de negocio en `app/services/` con transacciones claras y errores de dominio.

### Reglas de ejecución

1. Un caso de uso por método de servicio
2. Transacción explícita cuando haya múltiples operaciones de escritura
3. Validar reglas de negocio **antes** de persistencia
4. Lanzar excepciones de `app/core/exceptions.py` — nunca `HTTPException` directamente
5. Recibir `AsyncSession` como parámetro — no crear sesiones internas
6. Interactuar con storage/LLM solo via `Port` protocols inyectados

### Criterios de salida

- Services puros de negocio, testeables sin servidor HTTP
- Cobertura de pruebas unitarias para reglas principales
- Sin imports de `fastapi`, `boto3`, ni SDKs cloud

---

## Agente: Data and Migrations

### Objetivo

Evolucionar modelo de datos y migraciones Alembic sin romper compatibilidad.

### Reglas de ejecución

1. Cambios de modelo en `app/models/` — heredar siempre de `Base` con mixins (`UUIDMixin`, `TimestampMixin`, `SoftDeleteMixin`)
2. Generar migración: `make migration msg="descripcion_concisa"`
3. Revisar migración generada manualmente antes de commit
4. Evitar migraciones destructivas sin plan de rollback
5. Crear índices para columnas usadas en filtros frecuentes: `usuario_id`, `estado`, `mes`, `anio`, `contrato_id`
6. Registrar modelo en `app/models/__init__.py`

### Criterios de salida

- `alembic upgrade head` exitoso en local con PostgreSQL
- Migración legible, reversible y alineada al modelo
- Tests pasan con SQLite in-memory (aiosqlite)

---

## Agente: Auth and Security

### Objetivo

Garantizar autenticación JWT custom robusta y protección de endpoints por rol.

### Reglas de ejecución

1. **JWT custom con python-jose (HS256)** — NO usar JWKS, Cognito, Firebase ni proveedores externos
2. Access token: 15min, Refresh token: 7 días (configurable en `core/config.py`)
3. Validar `exp`, `sub` (UUID del usuario), `type` (access/refresh) en cada request
4. Passwords: bcrypt con cost factor 12 (`passlib[bcrypt]`)
5. Token blacklist en PostgreSQL (`TokenBlacklist` model) para logout/revocación
6. Aplicar `get_current_user` y/o `require_role` como dependencies en endpoints protegidos
7. Nunca hardcodear secretos — todo en variables de entorno
8. Encriptar tokens OAuth de terceros con Fernet (`TOKEN_ENCRYPTION_KEY`)

### Criterios de salida

- Endpoints protegidos correctamente por rol
- Errores uniformes: `401 Unauthorized`, `403 Forbidden`
- Tests para login, registro, refresh, logout, token expirado, token blacklisted

---

## Agente: Files and Evidence

### Objetivo

Gestionar evidencias y PDFs con storage S3-compatible desacoplado via `StoragePort`.

### Reglas de ejecución

1. Usar `StoragePort` protocol — **nunca importar boto3 directamente**
2. Presigned URLs para upload/download (expiración configurable)
3. Validar archivos: MIME type, extensión, tamaño máximo, magic bytes (`app/core/file_validation.py`)
4. Verificar ownership antes de cualquier operación
5. Convención de llaves: `{tipo}/{user_id}/{uuid}.{ext}` (e.g., `evidencias/{user_id}/{uuid}.pdf`)
6. Backends soportados: MinIO (dev), Cloudflare R2 (prod) — misma interfaz S3

### Criterios de salida

- Flujo completo: `presigned-upload → confirmar → listar → presigned-download`
- Tests con `moto[s3]` (mock de protocolo S3, no de AWS)
- Sin referencia directa a AWS/GCP/Azure en el código

---

## Agente: Payments

### Objetivo

Implementar cobros con consistencia financiera e idempotencia de eventos vía Wompi (Colombia).

### Reglas de ejecución

1. `Decimal` para todos los montos — nunca `float`
2. Verificar firma HMAC del webhook Wompi antes de procesar
3. Idempotencia por `referencia_externa` y/o `event_id`
4. Máquina de estados para pago: `PENDIENTE → APROBADO → FALLIDO / RECHAZADO`
5. Al aprobar pago → acreditar créditos al usuario
6. Registrar auditoría de cada transacción

### Criterios de salida

- Webhook seguro y repetible sin duplicar efectos
- Tests para firma válida/inválida, pagos duplicados, estados inválidos

---

## Agente: Admin and Reporting

### Objetivo

Entregar endpoints administrativos eficientes y seguros para alto volumen.

### Reglas de ejecución

1. Consultas agregadas optimizadas con índices
2. Exportaciones en streaming para datasets grandes
3. Restricciones estrictas por rol (`admin`) en todas las rutas
4. Paginación cursor-based para listas grandes

### Criterios de salida

- Dashboard con tiempos de respuesta estables (<200ms)
- Exportaciones funcionales sin consumo excesivo de memoria

---

## Agente: Testing and QA

### Objetivo

Asegurar calidad con pipeline reproducible en local y CI.

### Stack de testing

- `pytest-asyncio` con `asyncio_mode = "auto"` (no decorar tests con `@pytest.mark.asyncio`)
- `httpx.AsyncClient` con ASGI transport (sin servidor HTTP real)
- `aiosqlite` como DB in-memory para tests (no PostgreSQL)
- `moto[s3]` para mock de operaciones S3-compatible
- `factory-boy` para generación de datos de test
- Rate limiter deshabilitado globalmente en tests

### Reglas de ejecución

1. Unit tests para reglas de negocio (services)
2. Integration tests para endpoints críticos (API)
3. Mock de storage con `moto[s3]` — no levantar MinIO
4. Ejecutar siempre antes de commit: `make lint && make test`
5. Cobertura mínima: 70% (`make test-cov`)

### Criterios de salida

- Cambios sin regresión detectable
- Cobertura ≥70% en rutas y reglas críticas
- Zero warnings de ruff y mypy

---

## Agente: AI Agent / LangGraph

### Objetivo

Implementar y mantener el workflow de agente LangGraph para procesamiento de documentos, chat, recolección de evidencia y generación de documentos.

### Arquitectura del workflow

```
Input → [router] → chat mode     → [chat node] → END
                 → pipeline mode  → [doc_ingestion] → [doc_understanding] → [classification] → [justification] → END
                 → evidence mode  → [email_fetch] → [obligation_matching] → [justification_summary] → END
                 → drive mode     → [drive_upload] → END
```

### Modos del router

| Modo       | Palabra clave (LLM) | Trigger típico                                    |
| ---------- | ------------------- | ------------------------------------------------- |
| `chat`     | "chat"              | Preguntas conversacionales, consultas generales   |
| `pipeline` | "pipeline"          | "procesa este documento", "analiza este contrato" |
| `evidence` | "evidence"          | "busca evidencias", "revisa mis correos"          |
| `drive`    | "drive"             | "sube a Drive", "guarda en Drive"                 |
| `config`   | "config"            | Configuración de preferencias                     |

### Reglas de ejecución

1. Estado tipado via `AgentState` (TypedDict con `total=False`)
2. Nodos retornan state parcial: `{**state, "key": value}` (spread pattern)
3. Prompts versionados en `app/agent/prompts/` — nunca inline
4. LLM vía `LLMPort` protocol — nunca importar litellm/openai directamente
5. Email/Drive vía `EmailPort`/`DrivePort` protocol — nunca importar googleapiclient directamente
6. Tools en `app/agent/tools/` — funciones puras que operan sobre datos
7. Registrar token usage para sistema de créditos
8. Temperatura 0.0 para nodos de clasificación/routing, ≤0.3 para generación

### Criterios de salida

- Workflow determinístico y reproducible
- Tests para cada nodo individualmente
- Manejo de errores LLM (fallback chain, reintentos)
- `state["error"]` poblado en vez de excepción no capturada

---

## Agente: Evidence Collection

### Objetivo

Recolectar evidencia de cumplimiento contractual desde Gmail, mapearla a obligaciones específicas y generar justificaciones de actividades para la cuenta de cobro.

### Entradas esperadas

- `contrato_contexto`: `{fecha_inicio, fecha_fin, entidad, supervisor_email, numero}`
- `obligaciones_contexto`: lista de `{id, descripcion, orden}`
- `usuario_id`: para cargar tokens OAuth de Gmail

### Reglas de ejecución

1. Construir queries Gmail específicos por obligación (máx 5 queries por obligación)
2. Buscar emails con `EmailPort.search_messages()`, deduplicar por `message_id`
3. Matching LLM con temperatura 0.0: cada email contra cada obligación → `RELEVANTE|alta/media|razón` o `NO_RELEVANTE`
4. Truncar cuerpo del email a 800 chars antes del LLM (el subject + primer párrafo contiene el 90% de la señal)
5. Para evidencia encontrada → generar justificación con temperatura ≤0.3
6. Si no hay evidencia → indicar explícitamente, no inventar actividades
7. Usar modelo barato para matching (`groq/llama-3.1-8b-instant`), mejor para justificación (`gemini/gemini-2.5-flash`)
8. Limitar a 5-7 obligaciones por ejecución para respetar rate limits Gmail

### Criterios de salida

- `email_evidence`: lista estructurada `[{obligacion_id, emails: [{message_id, subject, relevance}]}]`
- `actividades_generadas`: lista `[{descripcion, obligacion_id, evidencia_ids}]`
- `response`: resumen narrativo para mostrar al usuario

---

## Agente: MCP Integration

### Objetivo

Construir e integrar MCP servers Python que expongan capacidades de Google Workspace al agente y a Claude Code como herramientas reutilizables.

### Entradas esperadas

- Servicio objetivo: Gmail | Drive | Calendar | (futuro: Outlook, Slack, Notion)
- Scopes OAuth requeridos
- Endpoints de la API backend a usar

### Reglas de ejecución

1. Crear el MCP server en `mcp_servers/{servicio}_server.py` usando `mcp[cli]`
2. El server llama a la API FastAPI (nunca directamente a Google) — auth centralizada
3. Definir tools con `@app.list_tools()` y `@app.call_tool()` con schemas JSON claros
4. Registrar en `.claude/settings.json` bajo `mcpServers`
5. Para nuevo servicio Google: crear `adapters/{servicio}/port.py` + `{servicio}_adapter.py`
6. OAuth: usar `OAUTHLIB_INSECURE_TRANSPORT=1` solo en dev, nunca en prod
7. Scope mínimo requerido: `drive.file` (no `drive`), `gmail.readonly` + `gmail.send`
8. Tokens: siempre encriptar con Fernet antes de guardar, desencriptar en memoria solo para llamadas API
9. Credenciales Google: nunca en código ni en git — siempre `.env`

### Criterios de salida

- MCP server ejecutable: `uv run python mcp_servers/gmail_server.py`
- Tools visibles en Claude Code vía `/mcp`
- OAuth flow completo: connect → callback → status → revoke
- Tests con credenciales mock o sandbox de Google

---

---

## Agente: SECOP Discovery & Onboarding

### Objetivo

Detectar contratos activos en SECOP II por cédula y descargar documentos asociados para poblar automáticamente el contexto del contratista.

### Entradas esperadas

- `cedula`: número de identificación del contratista
- O: formulario manual / PDF del contrato privado

### Reglas de ejecución

1. Usar `tools/secop_client.py` — cliente Socrata para dataset `jbjy-vk9h` (contratos) y `p6dx-8zbt` (documentos)
2. Filtrar por `estado=vigente` y `tipo_contrato=prestacion_de_servicios`
3. Descargar documentos referenciados a `documentos_fuente` via `StoragePort`
4. Si SECOP no responde → `state.error` descriptivo, no excepción cruda
5. Contrato privado: si el usuario sube PDF → procesar igual que SECOP (extracción completa); si llena formulario → persistir campos clave directamente
6. Mock Socrata en tests con fixture `tests/fixtures/secop_contratos.json`

### Criterios de salida

- `secop_contratos`: lista con metadatos del contrato (referencia, objeto, valor, vigencia, entidad)
- `secop_documentos`: lista de URLs de documentos disponibles
- `documentos_fuente` persistidos en DB con metadata correcta
- Tests: unit con mock, integración con cédula de prueba pública

---

## Agente: Template & Requirements Intelligence

### Objetivo

Comprender los requisitos de cuentas de cobro de cada entidad, construir su perfil y resolver qué plantillas usar, solicitando al usuario las que falten de forma ordenada.

### Reglas de ejecución

1. `requirements_ingestion_node`: usar `gemini/gemini-2.5-flash` con structured output → schema `EntityRequirements`
2. `entity_profile_node`: re-usar perfil existente si la entidad ya fue procesada; actualizar si llegan nuevos docs
3. `template_resolver_node`: buscar en tabla `plantillas` por `entidad_tipo + documento_tipo`
4. Si falta plantilla → `interrupt()` con mensaje HIL que incluye: nombre del documento, campos requeridos, ejemplo de nombre de archivo, cómo subir
5. Soportar logos (PNG/JPG), plantillas DOCX, PDF de ejemplo y archivos de instrucciones en texto
6. Detección automática: analizar membrete, logo, nombre de entidad del contrato para clasificar tipo de entidad

### Criterios de salida

- `entity_requirements`: schema normalizado con documentos requeridos, plazos, formato de evidencias
- `entity_profile_id`: UUID del perfil creado/actualizado en DB
- `template_id`: UUID de la plantilla resuelta o `None` (con HIL activo)

---

## Agente: Evidence Orchestrator (CrewAI)

### Objetivo

Coordinar la recolección paralela de evidencia desde múltiples fuentes usando CrewAI, maximizando recall con mínimo tiempo de ejecución.

### Crew: `EvidenceGatheringCrew`

| Agente | Herramientas | Responsabilidad |
|--------|-------------|----------------|
| `GmailSearchAgent` | MCP gmail_server | Búsqueda de emails por obligación + período |
| `DriveSearchAgent` | MCP drive_server | Búsqueda de archivos en Drive |
| `CalendarSearchAgent` | MCP calendar_server | Eventos del período como evidencia de reuniones |

### Reglas de ejecución

1. Lanzar `EvidenceGatheringCrew.kickoff_async()` desde `evidence_orchestrator_node`
2. Pasar contexto: `{obligaciones, periodo_inicio, periodo_fin, entidad_keywords}`
3. Timeout: 120 segundos por crew; si se supera → usar evidencia parcial recolectada
4. `evidence_matcher_node`: primero pgvector cosine (umbral ≥ 0.75), luego LLM refinement top-5
5. `evidence_dedup_node`: SHA-256 del contenido + cosine ≥ 0.95 para duplicados
6. Archivo `app/agent/crews/evidence_crew.py` — aislado, tests propios
7. Compatibilidad: pinning estricto de `langchain-core` compartida con LangGraph

### Criterios de salida

- `matched_evidence`: dict `obligacion_id → [evidencias rankeadas por relevancia]`
- `deduplicated_evidence`: sin duplicados por grupo
- Recall ≥ 85% sobre dataset benchmark (Fase 4)

---

## Agente: Document Assembly (CrewAI)

### Objetivo

Generar en paralelo todos los documentos de la cuenta de cobro usando plantillas resueltas, obligaciones y evidencias consolidadas.

### Crew: `DocAssemblyCrew`

| Agente | Responsabilidad |
|--------|----------------|
| `CuentaCobroAgent` | Genera el cuerpo principal de la cuenta de cobro |
| `InformeActividadesAgent` | Genera el informe de actividades con evidencias |
| `AnexosAgent` | Prepara los anexos y organiza documentos de soporte |

### Reglas de ejecución

1. **Vista previa obligatoria**: siempre generar HTML preview antes de PDF/DOCX
2. `GET /api/v1/cuentas-cobro/{id}/preview` → HTML renderizado con Jinja2
3. PDF/DOCX solo se genera tras aprobación explícita (endpoint o botón UI)
4. Usar plantilla resuelta por `template_resolver_node` — nunca improvisar formato
5. Incluir logo de la entidad si está en `plantillas.logo_url`
6. `folder_organizer_node`: estructura `{entidad_slug}/{referencia_contrato}/{YYYY-MM}/{tipo_doc}/`

### Criterios de salida

- `document_drafts`: lista con HTML de cada documento para preview
- `folder_manifest`: mapa `tipo_doc → path/URL` en S3/Drive/local
- Set completo para 5 entidades distintas pasa checklist 20 items (Fase 5)

---

## Plantilla Operativa (todas las tareas)

```
1. Entender caso de uso y constraints (rol, estado, reglas)
2. Definir/ajustar schema Pydantic de entrada/salida
3. Implementar service con validaciones de negocio
4. Exponer endpoint con dependencias de seguridad
5. Agregar tests unitarios + integración
6. Ejecutar: make lint && make test
7. Verificar impacto en migraciones/documentación
```

### Regla de oro

> El core de negocio (`services/`, `agent/`, `models/`) **nunca** importa SDKs de cloud.
> Solo interactúa con `Port` protocols definidos en `app/adapters/`.
