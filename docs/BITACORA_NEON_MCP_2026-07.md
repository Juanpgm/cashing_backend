# Bitácora de trabajo — Neon, regresiones y rediseño MCP

**Período:** 2026-07-06 → 2026-07-07
**Alcance:** estabilización local, features nuevas, cutover a Neon (Postgres), corrección de regresiones y primer bloque del rediseño MCP.

> Documento de referencia de todo lo hecho en este período. Estructurado por bloques cronológicos. Cada cambio referencia archivos y tests concretos.

---

## Índice

1. [Fix del block de arranque local](#1-fix-del-block-de-arranque-local)
2. [Reevaluación de tareas #3–#7](#2-reevaluación-de-tareas-37)
3. [Features nuevas](#3-features-nuevas)
4. [Setup de Neon (dev)](#4-setup-de-neon-dev)
5. [Fase A — corrección de regresiones](#5-fase-a--corrección-de-regresiones)
6. [Fase B — rediseño MCP (en progreso)](#6-fase-b--rediseño-mcp-en-progreso)
7. [Cómo correr en local](#7-cómo-correr-en-local)
8. [Pendientes / roadmap](#8-pendientes--roadmap)
9. [Gotchas y notas operativas](#9-gotchas-y-notas-operativas)
10. [Inventario de tests nuevos](#10-inventario-de-tests-nuevos)

---

## 1. Fix del block de arranque local

**Síntoma:** los endpoints de cuentas de cobro crasheaban en local.

**Causa:** el `dev.db` (SQLite) estaba desactualizado respecto a los modelos y, además, **bloqueado por dos procesos `pytest` zombie** (el puerto 8000 estaba libre — no era el server).

**Fix:** matar los pytest colgados → renombrar `dev.db` a backup → bootear para que `create_all` reconstruya el schema. Verificado: 27 tablas, `requisitos_modo`, `requisitos_cuenta`, `requisito_cuenta_id` presentes; `/docs` responde 200.

**Regla clave:** `create_all` crea **tablas** faltantes pero **NO agrega columnas** a tablas existentes. Tras cambios de modelo hay que respaldar/borrar el SQLite local. (Tablas nuevas sí se crean solas.)

---

## 2. Reevaluación de tareas #3–#7

La foto de estado tenía ~17 días y estaba muy desfasada. Verificado contra el código (71 tests verdes en esas suites):

| # | Estado real | Nota |
|---|---|---|
| #3 `/cruzar` + anti-alucinación | ✅ Ya hecho | `cruzar_service`, `quality_gate` (LLM-judge real, fail-open), prompts anti-alucinación |
| #4 wizard 3 pasos + semáforo | ✅ Ya hecho | `generar-tab.tsx` es wizard real (Analizar→Semáforo→Generar) |
| #5 supervisor + Constancia PDF | ✅ PDF hecho (WeasyPrint) | Firma criptográfica NO existía (ver §3) |
| #6 checkpointer + HIL + pgvector | ✅ Ya hecho | Motor **custom `CompiledGraph`**, NO LangGraph |
| #7 MCP/observabilidad | ✅ Parcial | Faltaba waitlist (ver §3) |

**Correcciones de rumbo importantes:**
- Deploy real: **Railway**, no GCP.
- Motor agéntico: **`CompiledGraph` custom** (`app/agent/engine.py`), no LangGraph.
- Embeddings: **`text-embedding-3-small` (1536)**, no `text-embedding-004` (Gemini sin créditos). Se corrigió el docstring en `embedding_service.py`.

---

## 3. Features nuevas

Todas construidas con tests y verdes en local SQLite.

### #7 — Gate de waitlist / invite-code
- Modelo `app/models/invite_code.py` (`codigo`, `max_usos`, `usos_actuales`, `activo`, prop `disponible`).
- Migración **`021_invite_codes`**.
- Setting `WAITLIST_ENABLED` (**off por defecto** → registro abierto sin cambios).
- Excepción `InviteRequiredError` → 403.
- `auth_service._consume_invite_code` — consumo atómico (rollback si falla), gate en registro email **y** primer Google sign-in.
- Campo `invite_code` en `RegisterRequest` / `GoogleAuthRequest`.
- Tests: `tests/test_waitlist.py`.

### #6 — Búsqueda semántica (doble backend)
- `app/services/semantic_search_service.py`: **pgvector `<=>` en Postgres** + **fallback coseno en Python en SQLite** (patrón de fallback del proyecto). Corre en local igual.
- Schema `ObligacionSimilar`.
- Endpoint `GET /api/v1/contratos/{id}/obligaciones/similares?q=&top_k=` (con ownership 403).
- Tests: `tests/test_semantic_search.py`.

### #7 — Notificaciones salientes (ports/adapters)
- `app/adapters/notification/` (port + `log_adapter` + `webhook_adapter` httpx).
- `app/services/notification_service.py` — canal pluggable (`log`|`webhook`), **fail-open** (nunca rompe el flujo).
- Settings `NOTIFICATIONS_ENABLED` / `NOTIFICATION_CHANNEL` / `NOTIFICATION_WEBHOOK_URL` (off por defecto).
- Enganchado en `pago_service.procesar_webhook_wompi` al aprobar pago (evento `pago.aprobado`).
- Tests: `tests/test_notifications.py`.
- **Gotcha:** structlog reserva `event` como el mensaje del log → usar `event_key` para campos custom.

### #5 — Firma PAdES de la Constancia
- `pyhanko==0.35.2` (agregado a `requirements.txt`).
- `app/services/pdf_signature_service.py` — gateado por `PDF_SIGNATURE_ENABLED` (off por defecto); cert configurable (`PDF_SIGNATURE_CERT_PATH/KEY_PATH/PASSPHRASE`) o **autofirmado efímero (SIN validez legal, etiquetado en el código)**.
- Integrado en `GET /cuentas-cobro/{id}/constancia.pdf`.
- Tests: `tests/test_pdf_signature.py`.
- **Gotchas:** usar `signers.async_sign_pdf` (await), NO el `sign_pdf` sync (choca con el event loop del request). WeasyPrint **no carga en Windows local** (falta GTK/pango) → los tests de constancia mockean la generación; la firma se prueba con un PDF armado por pyhanko.

### #4 — E2E del wizard
- `cashing-frontend/e2e/wizard-generar.spec.ts` — flujo Analizar→Semáforo→Generar con `page.route` mockeando **todas** las APIs (sin backend real) + token en localStorage.
- **Gotcha:** no mezclar tests sync y async en un mismo archivo (rompe el `event_loop` de sesión del conftest) — todos async.

---

## 4. Setup de Neon (dev)

**Proyecto:** `cashing-prototype` (`fragrant-wave-69296517`), org **Juan** (`org-late-lab-56653882`), **PostgreSQL 16.14**, región `sa-east-1`. pgvector 0.8.0 disponible.

**Ramas:**
- `production` (default/primary, **vacía — no se tocó**).
- `dev` (`br-frosty-moon-acaqoxba`, hija de production, endpoint `ep-autumn-union-acgvv03i`). **Solo se sincronizó `dev`.**

**Cómo se sincronizó dev:** NO por alembic — por **`create_all` desde los modelos** (igual que el local SQLite). 27 tablas idénticas al local + extensión `vector` instalada + `alembic stamp head` (`021_invite_codes`) para que el lifespan no falle. Local y Neon-dev quedan gemelas.

**Fix de SSL (código):** `app/core/db_ssl.py` nuevo — `prepare_pg_url(url)` hace strip de `sslmode`/`channel_binding` (asyncpg no los parsea) + decide SSL **por host** (localhost→off, remoto→ssl CERT_NONE). Reemplazó la lógica vieja por-environment en `database.py` y `alembic/env.py`. Antes ponía `ssl:False` salvo producción → Neon fallaba desde dev.

**Connection strings de dev** guardadas en `secrets/.env.local` (gitignorado): `DATABASE_URL_NEON_DEV_DIRECT` y `DATABASE_URL_NEON_DEV_POOLED`.

**Verificado:** app end-to-end contra Neon dev (INSERT+SELECT+DELETE de Usuario, directo Y pooled).

### ⚠️ HALLAZGO GRAVE — las migraciones alembic NO crean el schema base
Verificado: **ninguna** migración crea `usuarios`/`contratos`/`obligaciones`/`cuentas_cobro`/etc. Solo 8 migraciones crean tablas periféricas (secop, google_tokens, agent, requisitos, invite_codes). El schema base **siempre** se construyó con `create_all`. Por eso `alembic upgrade head` en una DB fresca **falla** (migración 002 hace `DELETE FROM obligaciones` y la tabla no existe). En la app real queda tapado porque el lifespan corre alembic (falla, se loguea) y después `create_all` construye todo. **Alembic hoy es decorativo.** → Antes de confiar en migraciones en Railway/prod hay que crear una **migración baseline** que capture el schema completo actual.

---

## 5. Fase A — corrección de regresiones

Regresiones destapadas al correr contra Neon (persiste datos; la SQLite local se reconstruía seguido y las tapaba). Todas corregidas con tests + **verificadas end-to-end contra Neon dev**.

### A1 — Créditos: fuente de verdad única
**Bug:** créditos en dos representaciones desincronizadas — cache `usuarios.creditos_disponibles` vs ledger `creditos` (SUM). `/auth/me` leía el cache; dashboard y `/creditos/balance` el ledger; `agregar_creditos` escribía solo el ledger (un top-up Wompi no tocaba el cache) → el gate de créditos podía bloquear tras pagar.

**Fix:** **ledger = verdad, cache = denormalización sincronizada.**
- `credito_service.py`: `_sync_cache` (setea cache = SUM ledger) llamado en `agregar_creditos`/`consumir_creditos`; `reconciliar_creditos`; `obtener_saldo` (int público).
- `/auth/me` (`auth_service.get_user_by_id`) y dashboard derivan el saldo de `obtener_saldo`.
- Desempate `order_by(created_at desc, id desc)` en `obtener_balance`.

**Verificado en Neon:** insert crudo de crédito dejando el cache drifteado → `/auth/me` igual mostró el ledger (50, no 30). Tests: `tests/test_creditos_sync.py`, `tests/test_dashboard.py`.

### A2 — SECOP traía pocos documentos
**Bug:** `sincronizar_documentos_secop` consultaba **un solo dataset (2025)** vía alias `_DS_DOCUMENTOS = _DS_DOCS_2025`; la otra función usaba los 4.

**Fix:** los helpers `_fetch_docs_for_contrato/_proceso` ahora usan `_query_docs_datasets` (fan-out de los 4 datasets 2018→hoy). `_query_docs_datasets` acepta `failed_out` → los datasets fallidos se surfacean en `SecopSincronizarDocumentosResult.datasets_con_error` (distingue "pocos docs" de "Socrata throttleó"). Aviso de arranque en `main.py` si `SECOP_APP_TOKEN` está vacío. Tests: `tests/test_secop_datasets.py`.

### A3 — Endurecimiento Postgres-vs-SQLite
- **Enum del dashboard (bug real de 500 en Neon):** el dashboard filtraba `estado.in_(["borrador","en_revision"])` — `en_revision` **no es label válida** del enum (los valores son borrador/enviada/aprobada/rechazada/pagada). En SQLite (enum como texto) no molestaba; en Postgres el enum nativo lo rechaza → 500. Fix: usar miembros del enum (`EstadoCuentaCobro.BORRADOR/ENVIADA/APROBADA`). **Verificado: `/dashboard` = 200 en Neon.**
- **`vector_search.py`:** hacía pgvector crudo sin branch de dialecto y se tragaba el error devolviendo `[]` (muerto en SQLite). Fix: branch `db.bind.dialect.name` (pgvector en PG / coseno Python en SQLite). Tests: `tests/test_vector_search.py`.

**Verificación Fase A:** 10 tests nuevos + ~150 de regresión verdes; end-to-end contra Neon (register 201, `/auth/me`=30, `/dashboard`=200, drift resuelto).

---

## 6. Fase B — rediseño MCP (en progreso)

**Dirección elegida:** tools modulares + pipelines donde convienen (híbrido, incremental). Plan completo en `.claude/plans/parsed-rolling-breeze.md`.

### Hecho (con tests, verde)
- **Token win — batch del `evidence_matcher`:** hacía hasta 5 llamadas LLM secuenciales por obligación → ahora **1 sola batch** (`_llm_relevance_batch`, devuelve array JSON de índices relevantes). ~5x menos round-trips. Tests: `tests/test_evidence_matcher_batch.py`. **Gotcha:** cambió el contrato del LLM (antes `RELEVANTE`/`NO_RELEVANTE`, ahora `[1,3]`) → se actualizaron mocks en `test_phase4_evidence.py` y `test_evidence_discovery.py`.
- **Token win — batch de `cruzar_service`:** `cruzar_documentos` hacía una llamada `_llm_relevance` **por candidato** (hasta 5 por obligación) → ahora `_llm_relevance_batch` clasifica todos los candidatos de una obligación en **1 llamada** (mismo contrato JSON `[1,3]` que el matcher). La justificación se mantiene por-candidato (grounded en una fuente, corre solo sobre los relevantes). Prompt nuevo `CRUZAR_RELEVANCE_BATCH_SYSTEM`; se borró el `CRUZAR_RELEVANCE_SYSTEM` per-candidato (quedó muerto). Tests: `tests/test_cruzar_service.py` (8, incluye selección parcial y fail-closed).
- **Higiene MCP:** `mcp_config.json` apuntaba a `app.mcp_servers.*` (mal) → `mcp_servers.*`; `evidence` registrado (estaba huérfano) + en agent_defaults; `filesystem_server.py` convertido a **FastMCP real**. Los 5 servidores importan con su instancia FastMCP.
- **MCP tools documentados y verificados:** los 4 tools de `filesystem_server` tenían docstring de una línea (sin `Args`/`Returns`) → enriquecidos al nivel de los demás, porque un `@mcp.tool()` sin descripción útil es inusable por el cliente MCP. Script de verificación `scripts/verify_mcp_tools.py` importa los 5 servidores, lista los tools vía `mcp.list_tools()` y falla si alguno tiene descripción vacía. Resultado: **13 tools en 5 servidores, todos con descripción y schema de parámetros**.
- **Config MCP corregida y sin crashes:** `.claude/settings.json` (el registro real que consume el cliente) tenía dos bugs que generaban errores de conexión: (1) el server HTTP `cashin` apuntaba a `http://localhost:9003/mcp` (puerto de un experimento viejo con el MCP inspector) cuando el backend real corre en **:8000** → corregido; (2) solo registraba 3 de 5 stdio servers (faltaban `drive`, `calendar`, `filesystem`) → ahora los **5 stdio + el HTTP** están registrados con env consistente. **Dos superficies MCP, ambas FastMCP-family:** HTTP vía `fastapi_mcp.FastApiMCP` montado en `/mcp` (`app/main.py`, solo en dev, sobre el puerto del backend) + los 5 stdio de `mcp_servers/`. Verificación de runtime real: `scripts/verify_mcp_runtime.py` **spawnea cada stdio server como subproceso, hace el handshake MCP `initialize` y lista tools** (no solo import) → los 5 arrancan sin crashear; el mount HTTP `/mcp` importa y monta sin excepción en dev.
- **Token accounting:** `agent_service.chat` devolvía `tokens_used=0` hardcodeado → ahora `chat_node` propaga `resp.total_tokens` al state y `chat()` lo reporta. Tests: `tests/test_chat_tokens.py`.
- **Dead code:** borrado `app/agent/crews/` (CrewAI sin referencias).
- **Visión:** los caps ya existen y son razonables (`MULTIMODAL_MAX_PDF_PAGES=8`, `MULTIMODAL_RASTER_DPI=150`) — no se tocaron.

### Pendiente de Fase B (lo grande, NO empezado)
- **Tool registry** — abstracción de tools con schema I/O estrecho por capacidad, empezando por las funciones puras sin LLM (SECOP download, checklist, extracción texto/OCR, embeddings, informe DOCX, dedup, carpetas). Es el núcleo arquitectónico; merece su propia ronda de diseño.
- **Auth MCP por-usuario** — hoy los servidores usan `CASHIN_BEARER_TOKEN` estático.
- Token accounting en **todos** los nodos (hoy solo el path de chat) — requiere acumulador a nivel del `LLMPort`.

---

## 7. Cómo correr en local

Scripts en la raíz del workspace (`C:\Users\User\Documents\workspace\cashing`):

| Comando | Base de datos |
|---|---|
| `.\start-local.ps1` | SQLite local (`dev.db`) |
| `.\start-local-neon.ps1` | **Neon dev** (lee `DATABASE_URL_NEON_DEV_DIRECT` de `secrets/.env.local`) |
| `.\kill-local.ps1` | Frena backend + frontend |

Ambos: backend `:8000`, frontend `:3000`. **El primer arranque contra Neon tarda ~60–90s** (región São Paulo + wake del compute + `create_all`/alembic por red) — es normal.

Todas las features nuevas arrancan **apagadas por defecto**; se prenden en `secrets/.env.local` o `.env`:
- `WAITLIST_ENABLED=true`
- `NOTIFICATIONS_ENABLED=true` (+ `NOTIFICATION_CHANNEL=log|webhook`)
- `PDF_SIGNATURE_ENABLED=true`

**Convención de tests:** `uv run python -m pytest ...` (NO `uv run pytest`). **UPDATE 2026-07-08: la suite completa ya corre VERDE entera** (`847 passed, 0 fallos`, ~80s) — ya no hace falta correr por grupos. Se resolvieron las causas reales del viejo "~136 fallos": (1) un **segfault nativo de python-magic** (`import magic`/libmagic en Windows+py3.14) que mataba el proceso al 59% y enmascaraba todo → `validate_mime_type` ahora usa firmas de magic-bytes manuales por defecto (libmagic opt-in por `FILE_VALIDATION_USE_LIBMAGIC=1`); (2) el fixture **`event_loop` deprecado** en `conftest.py` (roto en pytest-asyncio 0.26 + py3.14: `get_event_loop()` ya no auto-crea) → borrado, + `asyncio_default_fixture_loop_scope="function"`; (3) 2 bugs SECOP destapados: `_meses_calendario` off-by-one (`>=`→`>`) y falta de dedup por `id_documento` en `_query_docs_datasets` (fan-out a 4 datasets).

---

## 8. Pendientes / roadmap

**Operativos / decisiones:**
- **Rotar credenciales** pegadas en el chat: password de la DB Neon + API key `cashing-prototype`.
- **Migración baseline de alembic** — hoy no crean el schema base; necesario antes de confiar en migraciones en Railway/prod.
- Índice ivfflat de pgvector en Neon (create_all no lo crea; la búsqueda funciona sin él, seq-scan).
- `production` (Neon) sigue vacía — sincronizar cuando se decida.

**Decisiones de producto pospuestas:**
- Cert de entidad certificadora colombiana (Certicámara/Andes SCD) para firma con validez legal real.

**Fase B pendiente:** tool registry (núcleo arquitectónico), auth MCP por-usuario, token accounting global (ver §6).

---

## 9. Gotchas y notas operativas

- **Arranque: colisión create_all ↔ alembic (RESUELTO).** El lifespan (`app/main.py`) corría `alembic upgrade head` **antes** de `create_all`. Como `create_all` construye el schema base pero NO deja `alembic_version` stampeado, al re-arrancar alembic veía una DB sin versionar (o con `alembic_version` vacía tras un fallo previo — el DDL de SQLite autocommittea) e intentaba re-ejecutar la migración `001_secop_tables` desde cero → `OperationalError: table secop_contratos already exists` (traceback en cada boot, no fatal pero sucio; reventaría igual en Railway/prod). **Fix:** `create_all` primero (fuente de verdad del schema base, idempotente); luego alembic decide por estado real — si `alembic_version` no existe **o está vacía** → `stamp head` (marca sin re-ejecutar); si tiene fila de versión → `upgrade head` (aplica solo deltas). Verificado: primer boot stampea, siguientes hacen upgrade no-op, 0 tracebacks. Pendiente aún para prod: migración baseline real (que create_all deje de ser la fuente del schema).
- **`create_all` no altera columnas** de tablas existentes → tras cambios de modelo, borrar/respaldar el SQLite local.
- **Enums en Postgres usan los NOMBRES como labels** (`Enum(TipoCredito)` sin `values_callable` → labels COMPRA/CONSUMO/BONUS, no compra/consumo/bonus). La app usa siempre miembros del enum (ok); comparar una **columna** enum contra un string lowercase en una query SQL **falla en Postgres**. Para insertar crudo en `creditos` usar `'COMPRA'`.
- **Neon pooled endpoint** = PgBouncer transacción (no soporta prepared statements); en pruebas simples anduvo, pero el toggle usa el endpoint **directo** por robustez.
- **WeasyPrint no carga en Windows local** (falta GTK/pango) → constancia PDF no se genera en local sin instalar GTK; en Railway (Docker) funciona.
- **structlog:** `event` es palabra reservada del logger → usar otro nombre (`event_key`) para campos custom.
- **pyhanko:** en contexto async usar `async_sign_pdf`, no `sign_pdf`.
- **Tests async:** no mezclar sync y async en un mismo archivo (rompe el event_loop de sesión).

---

## 10. Inventario de tests nuevos

| Archivo | Cubre |
|---|---|
| `tests/test_waitlist.py` | Gate de invite-code (email + Google, consumo, agotado, inactivo) |
| `tests/test_semantic_search.py` | Búsqueda semántica servicio + endpoint (200 + ownership 403) |
| `tests/test_notifications.py` | Dispatch, canal, fail-open, adapter webhook, trigger pago |
| `tests/test_pdf_signature.py` | Firma PAdES servicio + endpoint (firmado/sin firmar) |
| `cashing-frontend/e2e/wizard-generar.spec.ts` | Wizard Analizar→Semáforo→Generar (mockeado) |
| `tests/test_creditos_sync.py` | Sincronización cache↔ledger, /auth/me desde ledger, reconciliar |
| `tests/test_dashboard.py` | Conteo de pendientes (enum) + saldo desde ledger |
| `tests/test_secop_datasets.py` | Sincronización sobre los 4 datasets + reporte de fallidos |
| `tests/test_vector_search.py` | Branch de dialecto (fallback Python) + validación de dimensión |
| `tests/test_evidence_matcher_batch.py` | Batch del matcher (1 llamada por obligación) |
| `tests/test_cruzar_service.py` | Cruzar docs→obligación + batch de relevancia (selección parcial, fail-closed) |
| `tests/test_chat_tokens.py` | Propagación de tokens reales en el path de chat |
| `scripts/verify_mcp_tools.py` | (no-pytest) Los 5 servidores MCP importan y exponen 13 tools con descripción y schema |
| `scripts/verify_mcp_runtime.py` | (no-pytest) Los 5 stdio servers spawnean, hacen handshake MCP `initialize` y listan tools sin crashear |
