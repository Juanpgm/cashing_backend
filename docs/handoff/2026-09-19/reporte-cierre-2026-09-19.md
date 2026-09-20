# Reporte de cierre — plan "radicar sin fricción" (2026-09-18 / 2026-09-19)

Estado: masters `cashing-backend-master` @ `81aab19` y `cashing-frontend` @ `11bf4c4`. Todo lo que aparece abajo fue verificado en esta sesión; lo que no se pudo verificar se dice explícitamente en la sección 8.

## 1. Resumen ejecutivo

- El cambio `radicacion-sin-friccion` quedó verificado (PASS WITH WARNINGS, 0 CRITICAL) y archivado en `openspec/changes/archive/2026-09-19-radicacion-sin-friccion/`.
- Se corrigieron y mergearon los 3 warnings de código del primer verify y dos hallazgos adicionales de las revisiones (backend PR #95; frontend PR #69, #70, #71).
- Suites y gates en verde en ambos repos; flujo completo verificado en vivo sobre un stack local con LLM falso.
- Producción: solo se pudo ejecutar la parte anónima y de solo lectura de la capa 1. **El build desplegado en producción NO incluye los PRs backend #94 ni #95** (ver sección 5). Las lecturas autenticadas y cualquier escritura no se ejecutaron (sección 5.4).
- Pendientes que requieren decisión o recursos del usuario: credenciales/URL para la capa 1 autenticada, decisión UX del grupo 2, Docker para la suite de PostgreSQL real, decisión de desplegar.

## 2. Cambios mergeados en esta etapa

| Repo | PR | Master | Qué resuelve |
|---|---|---|---|
| backend | #95 | `81aab19` | Reset de jobs a prueba de carreras (clasificación de evidencias y paquete): `updated_at` explícito, `populate_existing=True` bajo `FOR UPDATE`, y el reset de paquete anula el payload del run anterior |
| frontend | #69 | `7fddacb` | W3: errores de upload leídos vía `extractApiError` (barrido de `lib/**`, `eslint lib` = 0); W2: `ProgresoEtapas` con nombre accesible (`aria-label`) |
| frontend | #70 | `d369fbe` | `seedChecklist` de e2e lee el checklist real (tras el backend PR #94) con un piso de 4 códigos siempre aplicables; refuerzo del test de doble-click |
| frontend | #71 | `11bf4c4` | `contraste.test.ts` y `api.test.ts` dejan de fallar por inanición de CPU |

Hallazgos que salieron de las revisiones adversariales (no estaban en el verify original):

1. El `SELECT ... FOR UPDATE` sobre una fila ya cargada en el identity map de SQLAlchemy no refresca las columnas: sin `populate_existing=True` el segundo llamador conserva el `updated_at` viejo y vuelve a encolar. El primer arreglo (solo bump de `updated_at`) no cerraba la carrera. Mismo patrón sin arreglar en `secop_service.py:1443-1448`.
2. El reset de un job de paquete dejaba en una fila `pending` el payload del run anterior (`storage_key`, `listo_para_radicar`, ...), contra el contrato del schema.
3. El helper de e2e podía subir menos requisitos en silencio y ocultar una regresión del backend; se agregó el piso de 4 códigos.

## 3. Verificación ejecutada

| Chequeo | Resultado |
|---|---|
| Backend `uv run python -m pytest` en master | 3329 passed, 1 skipped, 15 deselected |
| Backend `scripts/pre-merge.ps1` | `GATE OK 81aab19 tests=3330 coverage=88% ruff=526 mypy=357 dirty=no` |
| Frontend `npm run gate` | `GATE OK 11bf4c4 unit=1389 e2e=7 tsc=clean lint=backlog(3e/8w) build=ok dirty=no` |
| Journey en vivo (`diez-ejercicios-radicacion`, stack local, LLM falso) | 13/13 tras el arreglo del helper (antes: 12/13, Ejercicio 8 fallaba 3/3 por fixture desactualizado tras el backend PR #94) |
| Casos borde en vivo | 18 passed, 1 skipped por diseño (`llm-respuesta-malformada`) |
| `radicacion-paquete-completo` + otros journeys que usan el helper | pasan (26 passed / 1 skipped junto con casos borde; 23/23 en los otros cinco journeys) |
| Carrera de jobs en vivo (PR #95) | al re-disparar un paquete ya `done`, el job pasa a `pending`/`running` con `storage_key` y `listo_para_radicar` en `null` y vuelve a `done` con paquete nuevo |
| Flaky `contraste.test.ts` bajo 64 procesos de carga | 18/40 → 0/40 (protocolo A); 4/8 → 0/8 (suite completa) |
| Flaky `step-2-cuota` (doble click) | diagnosticado como timing del test, no carrera de producto: 128 ejecuciones limpias; si se quita el guard, el test falla de forma determinista con "called 2 times" |

## 4. SDD

- Primer verify (2026-09-18): FAIL solo por exactitud de artefactos (`tasks.md` sin el PR #68 ni `7af14b5`; spec `stepper-ux` decía "all" y master usa `staged`).
- Se escribieron spec y design retroactivos (no existían), se corrigieron `tasks.md` (Fase 5, 8 slices, 55/55), specs, `design.md` (D13, D14) y se creó `apply-progress.md`.
- Segundo verify (2026-09-19): PASS WITH WARNINGS, 0 CRITICAL, 4 WARNING, 4 SUGGESTION; 10 hallazgos resueltos, 2 abiertos, 5 nuevos (de proceso/trazabilidad).
- Archivado: verificado a mano porque el agente archivador reportó éxito con el trabajo a medias (copió 3 archivos, reescribió `design.md` como un resumen de 42 líneas y afirmaba "listo para deploy"). Se completó la copia con verificación por hash (13 archivos), se restauró el `design.md` original (22.8 KB) y se corrigieron 4 afirmaciones falsas del `archive-report.md`.
- Queda una carpeta duplicada e idéntica en `openspec/changes/radicacion-sin-friccion/`; el borrado fue bloqueado por el harness y debe hacerlo el usuario (comando en la sección 9).

## 5. Producción (capa 1, solo lectura)

Doctrina del proyecto (`docs/prod-test-plan.md`): nada que escriba datos de negocio corre contra producción; el flujo destructivo se ejerce en local (capa 2). Por eso no se ejecutó el journey completo contra prod.

### 5.1 Chequeos ejecutados (todos GET)

| Chequeo | Resultado |
|---|---|
| `scripts/smoke_prod.py` (anónimo) | 0 fallos, 0 avisos: `GET /health` PASS (environment=production); `GET /api/v1/health/llm` PASS con `status=degraded`; lecturas autenticadas omitidas (sin credenciales) |
| `GET /health`, `/docs`, `/openapi.json` | 200; 0.31 s, 0.37 s, 1.41 s |
| Versión reportada | `0.2.1` (igual que local, por lo que no identifica el commit) |

### 5.2 Huella del build desplegado (OpenAPI de prod vs. master)

- Prod: 143 operaciones; master: 156. Las 13 que faltan en prod son las rutas `/api/v1/debug/*`, que `app/api/router.py:59` monta solo en `development|dev|local|test`: ausencia esperada, no es una brecha.
- Único cambio de contrato real: el campo `heredado` de `RequisitoChecklistItem` existe en master y **no** en prod. Ese campo lo introdujo el backend PR #94 (`46b73d6`, 2026-09-16).
- Prod sí expone `paquete/job`, `regenerar-async`, `stepper-state` y `agent/chat/stream` (Fase 2, PR #89, 2026-09-14).
- Conclusión: el build de prod es anterior al PR #94, así que **no contiene #94 ni #95**. Si contiene o no el #93 (descubrimiento semántico) no se puede saber desde el contrato porque no cambia el API.

### 5.3 Hallazgo de producción: cadena de LLM degradada

`GET /api/v1/health/llm` → `degraded`. Cadena configurada: `gemini/gemini-2.5-flash` (alcanzable, 433 ms), `groq/openai/gpt-oss-20b` (alcanzable, 135 ms), `gemini/gemini-2.0-flash` (**404: el modelo ya no está disponible** en Google). Hoy no rompe nada porque los dos primeros responden, pero el respaldo final está muerto. Es configuración del entorno de producción; no se modificó nada.

### 5.4 No ejecutado

- Lecturas autenticadas (`smoke_prod.py` con `SMOKE_USER`/`SMOKE_PASS` y `npm run test:e2e:prod`): requieren una cuenta de prueba dedicada en prod y la URL del frontend desplegado; no hay `.env.e2e.prod`.
- Cualquier flujo con escrituras en prod (register, contratos, cuentas, uploads, radicar, créditos, LLM real): descartado por la doctrina del proyecto.

### 5.5 Flujo completo contra PostgreSQL real (Neon dev, no producción)

Como producción no admite escrituras y faltaban credenciales, el usuario eligió correr el flujo completo con un stack local (LLM falso) contra la rama **dev** de Neon (PostgreSQL 16.15). Informe completo del agente: `scratchpad/neon-full-flow-2026-09-19.md` (copiado a `context/neon-full-flow-2026-09-19.md`). Producción no recibió ninguna petición en esta prueba.

- **PR #95 verificado en PostgreSQL real**: con dos sesiones concurrentes sobre un job stale, el código de `master` encola exactamente **1** vez en 13/13 escenarios (ambos servicios); una reimplementación del código previo al #95, en el mismo arnés, encola **2** veces. Ciclo de vida del job de paquete por la API viva: 7/7. `POST /radicar` concurrente: una sola transición real y segunda llamada idempotente, 5/5.
- **Playwright sobre Neon**: 33 pasaron, 15 fallaron, 2 skipped, 4 no corrieron. Ninguna falla es un bug de producto en PostgreSQL (0 errores HTTP 500, 0 errores de asyncpg/SQLAlchemy en el log; los únicos errores del driver son de `alembic` al arrancar). Las 15 caen en tres causas: (A) 11 por una carrera latente del frontend, (B) 3 por presupuestos de 60/90 s demasiado cortos para la latencia de Neon (la misma lógica pasa por API sin tope), (C) 1 por una espera fija de 400 ms en un helper.
- **Hallazgo de producto (causa A)**: `components/stepper/hooks/use-group-auto-advance.ts:70-75`: el efecto de "reanudar" se dispara la primera vez que `stepper-state` resuelve y **sobrescribe** el grupo activo con el `current_step` del servidor, deshaciendo un clic del usuario hecho antes de esa respuesta. Con SQLite resuelve en milisegundos y no se nota; con `stepper-state` a 3.4 s en Neon el asistente vuelve al Paso 3. Un usuario real con un backend lento (instancia fría) podría verlo.
- **Esquema**: deriva solo aditiva (falta la tabla `paquete_job`, creada por la propia app). `alembic upgrade` muere en `042` con `DuplicateTableError` porque `create_all` ya creó la tabla, y `043` nunca corre: alembic sigue siendo decorativo en una BD construida con `create_all`.
- **Herramientas**: `start-local-neon.ps1` (raíz del workspace) falla su propio guard porque lee `secrets/.env.local` del worktree `cashing-backend-master`, donde no está el DSN; `tests/conftest.py` no acepta un DSN con TLS (`sslmode`) y ejecuta `DROP SCHEMA public CASCADE` antes de cada prueba, unos 4–11 min por prueba contra Neon.
- **Tests del propio repo contra PostgreSQL**: de 18 pruebas de concurrencia del PR #95 seleccionadas, 9 pasaron, 0 fallaron y 9 dieron error de setup del fixture porque el host perdió la red hacia Neon (`getaddrinfo failed`, `ConnectionResetError`), no por aserciones. Cada prueba cuesta unos 3 minutos porque `conftest.py` hace `DROP SCHEMA public CASCADE` + `create_all` antes de cada una; "toda la suite verde en PostgreSQL" sigue sin demostrarse y requiere Docker (`scripts/test-postgres.sh`).
- **Residuos**: quedaron en Neon dev **49 usuarios** `e2e-*@example.com` con sus contratos, cuentas, documentos y 3 filas de `paquete_job` (creados por Playwright y por las sondas de API); no se borraron por ser una operación destructiva en cascada. Los datos preexistentes (2 usuarios no-e2e, 9 contratos) no se tocaron, y la base temporal `e2e_pg_*_test` se eliminó. `cashing-frontend/test-results/` contiene JWT de usuarios desechables (sin DSN) y sigue en disco, ignorado por git.
- **Sin credenciales en los informes**: se revisó por `neon.tech`, `npg_` y `postgresql://` y no hay coincidencias.

### 5.6 Corrección posterior: carrera "resume vs clic" del asistente (frontend PR #72, master `f7e69c4`)

Se corrigió el hallazgo de producto de la sección 5.5. `use-group-auto-advance.ts` ahora recibe `userNavSignal` (el contador que el shell solo incrementa en navegación del usuario), registra su valor la primera vez que ve un `cuentaId` y omite el resume si el contador cambió antes de que llegara `stepper-state`. Pruebas: 8 de 29 tests del hook y 2 de 6 del shell en RED con el código previo; 4 mutaciones probadas (una sobrevive y es equivalente); spec e2e nuevo `stepper-state-lento.spec.ts` que retiene la respuesta hasta que el test la libera (con el código previo falla 5/5 bajo carga, con el arreglo pasa 10/10); gate `GATE OK aacf1c8 unit=1420 e2e=7`. Revisión fresca: GO. Matiz documentado: mientras carga `stepper-state` solo es alcanzable el clic sobre el grupo en el que el usuario ya está, y contarlo como intención cancela el resume durante el resto de la visita; queda como follow-up de UX un aviso "seguí donde ibas".

### 5.7 Despliegue a producción (ejecutado el 2026-09-19, autorizado por el usuario)

**Backend**: Railway, proyecto `cashing_backend`, servicio `cashin-api`, deployment `c533ab53-6eab-4a80-83fa-6178fd4bf718` = SUCCESS, código `master` @ `81aab19` (incluye PRs #93, #94, #95 sobre el build anterior `a730acf`). **Frontend**: Vercel ya estaba en `f7e69c4` por auto-deploy desde `master`; no se tocó.

Cómo se despliega el backend (contra lo que dice `CLAUDE.md:153-155`): **manual con `railway up`**; no hay auto-deploy desde GitHub ni job de deploy en el repo. Por eso #93–#95 llevaban días mergeados sin salir.

Secuencia y evidencia:
1. Lectura de solo lectura de la BD de prod (sesión `read_only`): `alembic_version = 041_cdp_enum_uppercase`, tabla `paquete_job` ya existente (la creó `create_all`), flags `RPC/CDP/CONTRATO = False`. Es el **escenario A** del plan: sin intervención, el arranque habría intentado `alembic upgrade head`, la migración 042 habría chocado con `DuplicateTable` y la 043 no habría corrido, con el deploy "verde" y la función del PR #94 muerta en silencio (fallo de alembic = `warning`, no caída). Comportamiento reproducido antes en PostgreSQL 16 real (base desechable en Neon dev).
2. `alembic_version` 041 → 042 en prod (equivalente a `alembic stamp 042_paquete_job`), una fila, en una transacción con precondiciones y verificación previa al commit. Autorizado explícitamente por el usuario. Reversión: `UPDATE alembic_version SET version_num='041_cdp_enum_uppercase' WHERE version_num='042_paquete_job'`.
3. Variable de Railway `LLM_PRODUCTION_FALLBACK_MODEL`: `gemini/gemini-2.0-flash` (retirado, 404) → `gemini/gemini-3.6-flash` (existe en la lista de modelos de Google; la nombra el propio error del proveedor). Fijada con `--skip-deploys` para no redeployar el código viejo.
4. `railway up --service cashin-api --detach`. Estados: BUILDING → DEPLOYING → SUCCESS en unos 3 minutos.

Verificación posterior (solo lectura):
- Log de arranque: `database_ready` → `alembic_ok action=upgrade` → `agent_graph_ready`; sin `alembic_failed`. Única línea "sospechosa": una `StarletteDeprecationWarning` (`HTTP_422_UNPROCESSABLE_ENTITY` deprecado) con su línea de código; no es un error.
- Datos: `alembic_version = 043_checklist_primera_cuota_flags`; flags `RPC/CDP/CONTRATO = True`, `CEDULA/RUT` sin cambios; catálogo 14 filas.
- OpenAPI de prod: ocurrencias de `heredado` 0 → 1 (huella del PR #94).
- `/health` 200; `/api/v1/health/llm` de `degraded` a **`ok`** con los tres modelos alcanzables (530 ms, 142 ms, 654 ms).
- `scripts/smoke_prod.py` anónimo: 0 fallos, 0 avisos. Las lecturas autenticadas no se ejecutaron (no hay credenciales de una cuenta de prueba).

Riesgos y pendientes del despliegue:
- **Rollback degradado**: el deployment anterior (`7026919d`, `a730acf`) pasó a `REMOVED`, así que no hay `railway redeploy` posible. El camino real es reconstruir `a730acf` en un worktree limpio (`git worktree add <dir> a730acf --detach`) y `railway up` desde ahí. La migración 043 es idempotente y la app vieja funciona con los flags en `True`, por lo que no hace falta revertirla.
- **#93 activa `EVIDENCE_QUERY_EXPANSION_ENABLED` por defecto**: una llamada LLM adicional por descubrimiento de evidencias (costo y latencia sin medir). Vigilar tras el deploy; se apaga por variable.
- **Credencial expuesta**: durante la investigación, un agente ejecutó `railway variables --kv` y volcó `FIREBASE_SERVICE_ACCOUNT_JSON` (con la `private_key` de `firebase-adminsdk-fbsvc@cashing-9b4f6`) al transcript local de un subagente. Rotar la clave de la service account en Google Cloud IAM, actualizar la variable en Railway y revocar la clave anterior. Nunca usar `railway variables --kv`.
- **Causa raíz sin arreglar**: `create_all` es la fuente de verdad del esquema y alembic quedó decorativo; toda migración que cree una tabla volverá a colisionar en el próximo deploy. Merece su propio cambio (por ejemplo, migraciones idempotentes con `IF NOT EXISTS`).
- **Documentación falsa**: `CLAUDE.md:153-155` afirma "zero-config CI/CD from GitHub push". Corregirla o conectar de verdad el repo a Railway.
- Suite de PostgreSQL del repo y capa 1 autenticada de prod siguen sin ejecutarse (Docker apagado; faltan credenciales de prueba).

## 6. Reindexado (codebase-memory-mcp, modo full)

| Proyecto | Antes | Después |
|---|---|---|
| backend-master (`81aab19`) | 8961 nodos / 57 983 aristas | 10 738 / 71 185 |
| frontend (`11bf4c4`) | 1202 / 2690 | 2625 / 6478 |
| enlaces HTTP entre repos | 3 | 9 |

Los ADR de ambos proyectos no existían (la memoria decía que sí); se reconstruyeron con lo verificado en esta sesión.

## 7. Follow-ups rastreados (no bloqueantes)

- Sin heartbeat durante el batch de LLM en `_ejecutar_clasificacion`: un job puede pasar de 120 s siendo vigente (stale ≠ muerto).
- `secop_service.py:1443-1448`: misma trampa del identity map.
- Reloj de la app vs `func.now()` en `updated_at` (cosmético con umbral de 120 s).
- Backlog de lint: ruff 526, mypy 357.
- `checklist-full-view.test.tsx` ("chunks a drop of more than 20 files…"): 3/8 timeouts bajo carga; siguiente candidato a gate rojo espurio. Otros cerca del límite: `stepper-radicar` (3690 ms), `step-1-contrato` (2625 ms).
- `detail: ""` produce un error sin mensaje; selector ESLint de `.detail` global al repo.
- `scripts/kill-local.ps1` roto en PowerShell 7 (asigna a `$pid`, variable de solo lectura).
- `cashing-frontend/.env.local` apunta al puerto viejo :8003.
- Criterios de salida sin evidencia (UNVERIFIABLE): checklist <1 s en Neon, prueba con 3 usuarios, baselines de la Fase 0.

## 8. Lo que NO está verificado

1. **Suite de PostgreSQL real**: nunca corrió (Docker Desktop apagado). `FOR UPDATE` no hace nada en SQLite, así que la mitad del PR #95 que depende del bloqueo de fila no está probada por ejecución; solo el refresco del identity map y el `updated_at` explícito.
2. Lecturas autenticadas y escrituras en producción (sección 5.4).
3. Qué commit exacto corre en producción (solo se sabe que es anterior al #94).
4. El proceso `python -m pytest -q -p no:randomly` (PID 35196) visto activo desde otra shell: no se identificó su origen y no se detuvo.
5. Las rondas de corrección de tests e2e (helper y flaky) no tuvieron una segunda revisión fresca; están cubiertas por RED, mutación, ejecución en vivo y gate.

## 9. Acciones para el usuario

1. Decidir: (a) capa 1 autenticada (cuenta de prueba dedicada + URL del frontend desplegado) y (b) si se despliega master a producción. Antes de desplegar: correr `scripts/pre-merge.ps1 -IncludePg` con Docker encendido.
2. Decisión UX pendiente: altura del grupo 2 (colapsar por defecto / acordeón / dejar así); el riel `staged` la mitiga pero la decisión sigue abierta.
3. Reemplazar o quitar el tercer modelo de la cadena de LLM de producción (`gemini-2.0-flash`, retirado).
4. Borrar la carpeta duplicada: `Remove-Item -Recurse "C:\Users\User\Documents\workspace\cashing\openspec\changes\radicacion-sin-friccion"`.
