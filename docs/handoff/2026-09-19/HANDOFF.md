# Traspaso de sesión — CashIn — 2026-09-19

Documento para retomar el trabajo desde otra máquina (laptop) o en otra sesión de Claude Code. **Léelo entero antes de tocar nada.** Todo lo que aquí se afirma fue verificado durante la sesión del 18–19/09/2026; lo que no se pudo verificar está marcado.

## 1. Estado al cierre

| Pieza | Estado |
|---|---|
| Backend `Juanpgm/cashing_backend` | `master` = `b65f872` (PR #96). Trabajar SIEMPRE en el worktree `cashing-backend-master/`; `cashing-backend/` es un checkout obsoleto. |
| Frontend `Juanpgm/cashing_frontend` | `master` = `f7e69c4` (PR #72). |
| Producción backend (Railway) | Proyecto `cashing_backend`, entorno `production`, servicio `cashin-api`, deployment `c533ab53-6eab-4a80-83fa-6178fd4bf718` = **`81aab19`** (PRs #93, #94, #95). **No incluye el PR #96** (no requiere desplegarse todavía). `alembic_version = 043_checklist_primera_cuota_flags`. |
| Producción frontend (Vercel) | Proyecto `cashing-frontend`, auto-despliega desde `master`; en producción = `f7e69c4`. |
| Gates | Backend `GATE OK 4b95318 tests=3492 coverage=88% ruff=526 mypy=357` (contenido idéntico al squash `b65f872`). Frontend `GATE OK aacf1c8 unit=1420 e2e=7 tsc=clean build=ok`. |
| SDD `radicacion-sin-friccion` | Verify **PASS WITH WARNINGS** (0 CRITICAL) y **archivado** en `openspec/changes/archive/2026-09-19-radicacion-sin-friccion/`. Specs en `openspec/specs/`. |

## 2. Qué se hizo en esta etapa (18–19/09)

- **Backend**: PR #95 (reset de jobs a prueba de carreras: `populate_existing` bajo `FOR UPDATE`, `updated_at` explícito, payload anulado), PR #96 (migración 042 idempotente, guardián de migraciones, `scripts/check_alembic_state.py`, `docs/deploy-runbook.md`, `CLAUDE.md` corregido).
- **Frontend**: PR #69 (nombre accesible del progressbar, errores vía `extractApiError`), #70 (`seedChecklist` lee el checklist real + piso de 4 códigos), #71 (tests con inanición de CPU), #72 (el resume del asistente ya no pisa un clic hecho antes de que llegue `stepper-state`).
- **SDD**: spec y design retroactivos, `tasks.md` (Fase 5), `apply-progress.md`, dos verify, archivado.
- **Despliegue a producción** (autorizado): stamp de `alembic_version` 041→042 en prod (write puntual autorizado), variable `LLM_PRODUCTION_FALLBACK_MODEL` → `gemini/gemini-3.6-flash`, `railway up`, migración 043 aplicada (flags `RPC/CDP/CONTRATO = True`), `/health/llm` de degraded a ok.
- **Verificación**: flujo completo en local (LLM falso) y contra PostgreSQL 16 real (rama dev de Neon): PR #95 encola 1 vez en 13/13 escenarios concurrentes (el código previo encola 2). Limpieza de 49 usuarios `e2e-*` en Neon dev.
- Informes detallados: `reporte-cierre-2026-09-19.md`, `neon-full-flow-2026-09-19.md`, `adr-codebase-memory-2026-09-19.md` (en esta misma carpeta).

## 3. Pendientes, en orden (plan por fases)

**Fase 0 — URGENTE (el usuario, ~15 min): rotar la clave de Firebase.** Un agente ejecutó `railway variables --kv` y volcó `FIREBASE_SERVICE_ACCOUNT_JSON` (con la `private_key` de `firebase-adminsdk-fbsvc@cashing-9b4f6`) a un transcript local. Crear clave nueva en Google Cloud IAM (proyecto `cashing-9b4f6`), actualizarla en Railway por stdin (`railway variables --service cashin-api --set-from-stdin FIREBASE_SERVICE_ACCOUNT_JSON`) y revocar la vieja. Hecho cuando `GET /health` sigue en 200 tras el redeploy.

**Fase 1 — Suite de PostgreSQL real** (requiere Docker Desktop): `scripts/pre-merge.ps1 -IncludePg`. Nunca se ejecutó completa; incluye `TestReadOnlyOnPostgres` (garantía READ ONLY de `check_alembic_state.py`, jamás ejecutada). `tests/conftest.py` no acepta DSN con TLS.

**Fase 2 — Capa 1 autenticada en prod**: crear una cuenta de prueba dedicada; correr `scripts/smoke_prod.py` con `SMOKE_USER`/`SMOKE_PASS` y `npm run test:e2e:prod` (solo lecturas). Faltan credenciales y la URL del frontend.

**Fase 3 — Decisiones de producto**: altura del grupo 2 del asistente (colapsar / acordeón / dejar el riel `staged`); si se quiere el aviso "seguí donde ibas" cuando se cancela el resume.

**Fase 4 — Deuda con riesgo real**: (1) `secop_service.py:1443-1448`: mismo defecto del identity map que arregló #95; (2) sin heartbeat durante el batch de LLM en `_ejecutar_clasificacion` (un job vivo de >120 s se ve como stale); (3) `checklist-full-view.test.tsx` "chunks a drop of more than 20 files…" cae ~3/8 bajo carga; (4) presupuestos de 60/90 s y una espera fija de 400 ms en specs vivos que fallan por la latencia de Neon.

**Fase 5 — Higiene**: `scripts/kill-local.ps1` roto en PowerShell 7 (asigna a `$pid`); `start-local-neon.ps1` (raíz del workspace) lee los secretos del sitio equivocado; `.env.local` del frontend con puerto viejo :8003; `cashing-frontend/test-results/` con JWT de usuarios desechables; ~10.5k blobs huérfanos en `local_storage`.

**Fase 6 — Proceso**: borrar la carpeta duplicada `openspec/changes/radicacion-sin-friccion/` **del workspace** (la raíz del workspace no es un repo git; ver §6); reindexar el grafo de código y restaurar los ADR (§5); registrar el PR #93 en el spec (no tiene requisito propio).

**Fase 7 — Próximo despliegue** (decisión del usuario): seguir `docs/deploy-runbook.md`. Vigilar costo/latencia de `EVIDENCE_QUERY_EXPANSION_ENABLED` (el #93 lo activa por defecto: una llamada LLM extra por descubrimiento).

Fuera de alcance a propósito: backlog de lint (ruff 526 / mypy 357, no bloqueante) y quitar la dependencia de `create_all` como fuente de verdad del esquema (rediseño propio).

## 4. Reglas de trabajo vigentes (del usuario)

- **TDD proporcional** (desde 19/09): RED-first, mutación y revisión adversarial solo en rutas críticas (dinero, concurrencia, pérdida de datos, migraciones de producción, seguridad, máquina de estados de radicación). En lo demás: test junto al código, gate verde, una ronda de revisión. Los **casos borde siempre** se escriben y quedan en verde.
- **Local primero**: probar todo en local antes de cualquier deploy/push que dispare builds; pedir OK explícito antes de desplegar o de escribir en producción.
- Gate local obligatorio antes de push/merge (GitHub Actions está bloqueado por facturación): `scripts/pre-merge.ps1` (backend) y `npm run gate` (frontend), con hook `pre-push` (`make hooks` / `npm run hooks`). El PR debe pegar la línea `GATE OK <sha> … dirty=no`.
- Commits convencionales, **sin** `Co-Authored-By` ni atribución de IA.
- Nada que escriba datos de negocio corre contra producción (`docs/prod-test-plan.md`): el flujo destructivo va en local o en la rama dev de Neon.
- Comandos de test del backend: `uv run python -m pytest` (NUNCA `uv run pytest` a secas: segfault de python-magic en Windows).

## 5. Gotchas críticos (detalle en `claude-memory/`)

1. El deploy del backend es **manual** (`railway up --service cashin-api --detach -m "…"`); mergear a `master` no despliega nada. El frontend sí auto-despliega.
2. El arranque hace `create_all` **y luego** `alembic upgrade head`; un fallo de alembic es solo un `warning` (no cae el contenedor). Tras cada deploy verificar `alembic_ok` en el log y `alembic_version` (`scripts/check_alembic_state.py`).
3. El deployment anterior queda `REMOVED`: no hay redeploy de rollback; se reconstruye desde un worktree en el commit previo.
4. **NUNCA** `railway variables --kv` (vuelca todos los secretos). Nombres solo: `railway variables --service cashin-api | rg "^║ [A-Z]"`.
5. `select(...).with_for_update()` sobre una fila ya cargada en el identity map **no refresca columnas**: hace falta `.execution_options(populate_existing=True)`; y reasignar el mismo valor no emite UPDATE, así que `onupdate=func.now()` no dispara.
6. El timeout de vitest es de **reloj de pared**: un proceso sin CPU falla tests de 10 ms. Las guardas de carreras en e2e deben retener la red hasta que el test la libere, no usar un retraso fijo.
7. `/health` devuelve siempre `version 0.2.1`: no identifica el commit. Huella real: un campo o ruta nuevo en `/openapi.json` (p. ej. `heredado` del PR #94).
8. Un `index_repository` en modo `full` (codebase-memory-mcp) **borra el ADR** del proyecto: restaurarlo desde `adr-codebase-memory-2026-09-19.md`.
9. Enums de Postgres: sin `values_callable` las etiquetas van en MAYÚSCULA (nombres); con `values_callable`, en minúscula. Mirar el `mapped_column` antes de escribir una migración.
10. `local_dev.db` (SQLite) no gana columnas nuevas con `create_all`: respaldar/borrar y reiniciar tras cambios de modelo.
11. `alembic` es decorativo en una BD construida con `create_all`: las migraciones que creen tablas/índices/columnas deben ser idempotentes (helpers en `app/core/migration_helpers.py`; lo exige `tests/test_migration_convention_guard.py`; `make migration` genera archivos con id hash: hay que editarlos).
12. `git worktree`: `Agent(isolation:"worktree")` clona el repo del cwd actual, no el mencionado en el prompt.

## 6. Qué NO viaja por git (recrear o copiar a mano en la laptop)

- **La raíz del workspace no es un repo git.** Su `openspec/` y `context/` no se sincronizan. Lo relevante se copió a este repo: `openspec/changes/archive/2026-09-19-radicacion-sin-friccion/`, `openspec/specs/` y esta carpeta.
- `context/DAGMA` y `context/SYJ` (~105 MB, documentos reales de clientes): **no se suben por diseño** (datos de clientes). Los specs actuales usan fixtures sintéticos; no hacen falta.
- Archivos de entorno y secretos (no versionados): `.env` / `secrets/.env.local` (backend; recrear desde `.env.example`; en esta sesión se usaron al menos `DATABASE_URL_NEON_DEV_DIRECT` y una `GEMINI_API_KEY`), `.env.local` (frontend), `.env.e2e.prod` (opcional, para la capa 1 autenticada). Ninguno contiene valores en este repo.
- Memoria de Claude (`~/.claude/projects/<slug>/memory/`) y la base de Engram: son locales a cada máquina. Se exportó lo esencial a `claude-memory/` (ver su `README.md`). Observaciones de Engram relevantes (proyecto `cashing-backend`): #639 checkpoint maestro; topics `sdd/radicacion-sin-friccion/verify-report`, `…/verify-warnings-fix`, `ops/prod-fingerprint-2026-09-19`, `ops/neon-dev-full-flow-2026-09-19`, `bugfix/stepper-resume-vs-click-race`, `ops/deploy-plan-2026-09-19`, `ops/prod-deploy-2026-09-19`, `architecture/idempotent-migrations-guard`.
- `local_dev.db`, `local_storage/`, `test-results/`, `.venv`, `node_modules`.
- Herramientas: `gh auth login`, `railway login`, `npx playwright install`, `uv sync`, `npm ci`, Docker Desktop (Fase 1).

## 7. Respaldos subidos para no perder trabajo (ramas `backup/*`)

Estas ramas **no** son PR ni entran a `master`; existen solo para que el trabajo sin remoto no se pierda al cambiar de máquina. Se subieron con `SKIP_GATE=1` (no hay nada que integrar).

| Repo | Rama de respaldo | Qué contiene |
|---|---|---|
| backend | `backup/integracion-stepper-local-wip-2026-09-19` | Snapshot (30 archivos, +6974/−538) del trabajo **sin commitear** del checkout obsoleto `cashing-backend/` (rama `integracion-stepper-local`, de antes del 03/09; 323 commits detrás de `master`). Probablemente en su mayoría superado por lo mergeado; revisar antes de descartar. |
| backend | `backup/fix-evidencias-descubrir-contexto-contrato`, `backup/pr-review-36`, `backup/pr39-head-local` | Ramas locales que nunca se subieron (9, 11 y 15 commits). |
| frontend | `backup/refactor-phase1-error-codemod-app` | Rama local nunca subida (13 commits). |

Sin respaldar a propósito: ramas locales `copilot/*` de abril; ~25 (backend) y ~14 (frontend) ramas cuyo remoto se borró tras el merge (`git fetch --prune` y `git branch -vv | rg ': gone'` para limpiarlas); un stash antiguo del frontend (`stash@{0}` sobre una rama `worktree-wf_…`, ajeno a esta sesión); `uv.lock` modificado en el worktree `cashing-backend-prod` (rama `feat/agente-flujo-e2e`, ya subida). El repo `seismic_disaster_data_analisys_cali` es otro proyecto y **no se tocó**.

## 8. Cómo retomar en la laptop (paso a paso)

1. `gh auth login`; clonar `Juanpgm/cashing_backend` y `Juanpgm/cashing_frontend`; `git checkout master && git pull`. Backend: `make setup` (instala dependencias y el hook). Frontend: `npm ci && npm run hooks && npx playwright install chromium`.
2. Recrear los archivos de entorno (§6). Para probar en local: backend con `LLM_PROVIDER=fake DATABASE_URL=sqlite+aiosqlite:///./local_dev.db STORAGE_PROVIDER=local RATE_LIMIT_ENABLED=false WAITLIST_ENABLED=false`, frontend con `NEXT_PUBLIC_API_URL=http://localhost:8000` puesto en el proceso.
3. Verificar que todo está sano: `uv run python -m pytest` y `scripts/pre-merge.ps1` (backend); `npm run gate` (frontend). Esperado: backend 3490 passed / 2 skipped (el gate cuenta 3492); frontend unit 1420, e2e 7.
4. Restaurar la memoria de Claude si se quiere (`claude-memory/README.md`) y leer `reporte-cierre-2026-09-19.md`.
5. Empezar por la Fase 0 (rotar la clave de Firebase) y seguir el orden de §3.
6. Para producción: `railway login`, `railway status` (debe mostrar `cashing_backend / production / cashin-api`) y seguir `docs/deploy-runbook.md`. **No desplegar sin OK explícito del usuario.**
