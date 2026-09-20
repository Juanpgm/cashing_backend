---
name: plan-radicacion-sin-friccion
description: "Plan de mejoramiento 2026-09-12 en 5 fases (cimientos, cerrar flujo, velocidad, UX 3 pantallas, casos borde + Playwright) para que CashIn radique cuentas de cobro de punta a punta"
metadata: 
  node_type: memory
  type: project
  originSessionId: 41a46c5d-260f-4dcd-9f01-448d2d7da506
  modified: 2026-09-15T23:40:48.607Z
---

Artifact publicado: https://claude.ai/code/artifact/e9c67d19-e335-4cab-9cc9-d7f3e5a7b513 (fuente HTML en el scratchpad de la sesión 41a46c5d). Engram: topic `plan/radicacion-sin-friccion` (#637, proyecto cashing-backend) con el detalle de hallazgos file:line.

Los 4 bloqueantes que ordenan todo el plan (verificados 12/09 sobre master 7d33352 / frontend 49fcf69):
1. El wizard NUNCA llama a POST /radicar: "Finalizar" del paso 7 solo cierra el modal; el único botón real está en el grupo 2 (`checklist-full-view.tsx:367`).
2. El agente gasta 18 de 20 iteraciones en el happy path, reenvía ~12.5k tokens de prompt por iteración y NO puede persistir evidencias de Google (truncado 12k chars + max_tokens 1024). Le faltan tools de escape (no_aplica, stepper-state, PDF de cuenta, editar actividad).
3. Backend: cascada `lazy="selectin"` en Contrato/CuentaCobro (8 queries por entidad, pendiente desde julio), checklist 36-40 queries, radicar 60-80, y WeasyPrint/docx/zip SÍNCRONOS con `--workers 1`.
4. Cero CI; Playwright depende de documentos reales de clientes en `context/SYJ|DAGMA`; sin stub de LLM para la app corriendo.

**Why:** el usuario pidió un plan integral (agente + velocidad + UX + edge cases + Playwright). Sin cerrar el bloqueante 1 nada de lo demás llega a "radicada".

**How to apply:** ejecutar en el orden de PRs del artifact (0.1→0.2→0.3→0.5→0.4→1.1…); trabajar SIEMPRE en `cashing-backend-master` ([[gotcha-backend-checkout-stale]]); cada slice con casos borde nombrados; local primero ([[feedback-local-first]]); fixture de conteo de queries antes de cualquier optimización ([[perf-db-round-trips]]).

## Estado 2026-09-13: 11/32 slices, Fase 0 completa y mergeada

**Fase 0 (5/5) MERGEADA a master en ambos repos**: 0.1 (repoint scripts, sin PR), 0.2 fixture de queries (backend PR #66), 0.3 instrumentación del agente (backend PR #67), 0.4 FakeLLMPort (backend PR #68), 0.5 CI (backend PR #65, frontend PR #41). Fase 1 slice 1.1 (wizard radica de verdad, 4 rondas de revisión adversarial) MERGEADA (frontend PR #43). 4.8 adelantado a medias (Playwright en CI, 2 de 8 specs mockeados, frontend PR #42 MERGEADO).

**Próxima acción concreta: slice 1.2** — `radicar` idempotente y a prueba de doble-submit (`cuenta_cobro_service.py:1021-1088`).

**Detalle completo, decisiones, hallazgos de cada ronda de revisión**: `openspec/changes/radicacion-sin-friccion/tasks.md` (fuente de verdad) + engram topic `sdd/radicacion-sin-friccion/state` (checkpoint maestro, léelo primero en una sesión nueva) + ADR en codebase-memory-mcp para `cashing-backend-master` y `cashing-frontend` (`manage_adr(mode='get')`).

**Gotchas nuevos de esta ronda**: [[gotcha-structlog-cache-logger-processors]] (3 ocurrencias, nunca `monkeypatch.setattr` directo sobre un logger de structlog) y [[gotcha-agent-worktree-isolation-cwd]] siguen vigentes.

**Bloqueante operativo sin relación al código**: la cuenta de GitHub Actions de `cashing_backend` está bloqueada por facturación — el PR #65 falló su propio CI por eso, no por el código. Bloquea CI real en cualquier PR futuro hasta que el usuario lo resuelva en GitHub.

**Autorización vigente**: el usuario autorizó explícitamente mergear PRs "cuando estén listos" (2026-09-13) — no hace falta volver a preguntar por cada merge individual, solo verificar que pasó revisión adversarial + suite completa en verde.

## Estado 2026-09-15: Fase 2 (performance) CERRADA — 2.1 a 2.8 todos mergeados

Backend master en `91ba19c` (PR #90), frontend master en `b4fdda1` (PR #63). Mergeados desde el estado anterior: 4.2, 2.1, 2.2+2.3, 2.4a, 2.4b (cerrado como no-op), 2.5, 2.6, 2.7, 2.8-backend; 3.1, 3.2, 3.6, 3.8, 3.7, 3.9, 2.8-frontend.

**2.7 (paquete como job en background) — hallazgo notable**: BLOCKER real de concurrencia por un gotcha de SQLAlchemy — reasignar un atributo ORM al mismo valor que ya tiene NO emite UPDATE (`History.from_scalar_attribute` compara con `==`), así que `onupdate=func.now()` no se dispara y el reloj de "stale" de un job nunca se resetea en el caso de un `pending` atascado. Detalle completo en Engram `gotcha/sqlalchemy-noop-reassignment-onupdate` (#697) — también afecta latentemente a `evidence_classification_service._upsert_job`, ya mergeado, pendiente de arreglar ahí también.

**2.8 (cliente httpx compartido + higiene de queries) — cierra Fase 2**: un stop-hook a mitad de slice señaló correctamente que el objetivo original prioriza stepper+agente+Playwright, no infra/perf — se preguntó al usuario si terminar 2.8 o pivotar, eligió terminar 2.8. Dos auditorías previas evitaron implementar mal: "lazy per-row usePaquete" copiando el patrón `enabled: expanded` del checklist hubiera dejado el botón de descarga permanentemente deshabilitado (es siempre visible, no depende de expandir la fila) — se usó un hook `useInView` (IntersectionObserver) en su lugar. Se investigó y se DESCARTÓ un endpoint batch/JOIN para el estado de paquete por fila: `PaqueteJob` no es la fuente de verdad semántica del estado actual (existencia = HEAD de storage en vivo, listo_para_radicar = recómputo en vivo) — un JOIN cambiaría corrección real por un snapshot potencialmente obsoleto. Revisión backend encontró que el cliente httpx compartido para el endpoint de OAuth de Microsoft (`graph-token`) filtraba cookies de sesión ENTRE USUARIOS DISTINTOS — arreglado sin compartir ese cliente en particular (no hay caso de performance real para OAuth de baja frecuencia).

**Restante exacto (grepeado de tasks.md)**: 3.3/3.4/3.5 (rediseño de 3 pantallas del stepper — el bloque grande, YA DESBLOQUEADO, es lo que sigue), 4.3 (tests de fallas de red), 4.8 parcial (job de CI completo, dormido hasta que el usuario resuelva facturación de GitHub Actions).

**IMPORTANTE — reenfoque**: el `/goal` original pide explícitamente stepper+agente 100% verificados con Playwright (mínimo 10 casos). Fase 2 fue trabajo de plan legítimo pero es infra/performance backend, no el stepper ni el agente. 3.3/3.4/3.5 SÍ son el stepper — priorizar eso ahora, con verificación Playwright real tejida en el proceso, no pospuesta (ya existe `e2e/journey/diez-ejercicios-radicacion.spec.ts` de slices anteriores — extenderla, no repostergarla).

**Checkpoint maestro actualizado**: Engram `sdd/radicacion-sin-friccion/state` (#639, proyecto `cashing-backend` — no `cashing-backend-master`, son proyectos distintos en Engram para este repo).

## Estado 2026-09-15 (cont.): slice 3.3 mergeado — arranca el rediseño real del stepper

Frontend master `b77fb9e` (PR #64). Sin `design.md` en ningún lado para el rediseño de 3 pantallas — las viñetas de `tasks.md` son la especificación completa; se preguntó al usuario 3 decisiones de alcance antes de implementar (AskUserQuestion): Pantalla 1 = grupo 1 actual entero (contrato+cuota), la decisión de modo de checklist se muda ahí también, campos avanzados a decisión propia (número de cuota + fecha de transacción ocultos, informe final visible). Hallazgo arquitectónico clave: como Pantalla 1 = grupo 1 y Pantalla 3 = grupo 4 exactamente, lo ÚNICO que realmente reestructura grupos en 3.3/3.4/3.5 es fusionar grupos 2+3 en Pantalla 2 — eso es trabajo exclusivo de 3.4.

Revisión (2 rondas) probó empíricamente 2 bugs CRITICAL preexistentes de pérdida de datos en `DefinirChecklistGate` (no introducidos por esta slice, pero en el mismo archivo/área que se estaba tocando): una fila de checklist agregada manualmente desaparecía en silencio del payload real (sin input de código en la UI, backend descarta silenciosamente ítems con código vacío); mismo root cause dejaba "Aplicar checklist" permanentemente deshabilitado tras una inferencia vacía. Arreglado con derivación+dedup de código. Ronda 2 encontró que el propio arreglo no tenía tope de longitud, mientras la columna backend es VARCHAR(50) sin truncar — un nombre de requisito real (60+ caracteres) tumbaría el batch COMPLETO en Postgres real, invisible en SQLite local. Este patrón ("la ronda 2 encuentra algo que el propio arreglo de la ronda 1 introdujo") ya es la norma para hallazgos BLOCKER/CRITICAL en esta sesión, no la excepción.

**Restante exacto**: 3.4 (Pantalla 2, la fusión de grupos real), 3.5 (Pantalla 3, casi un re-etiquetado del grupo 4 actual), 4.3, 4.8-parcial.

## Estado 2026-09-15 (madrugada 16/09): 3.4 MERGEADO (frontend PR #65, squash `1ef7b14`) + gate local sin GitHub Actions

3.4 validado en 3 capas antes de mergear: Playwright vivo (13 specs, 41/43 → 43/43) halló un bug real nuevo (el `<select>` de obligación empujaba "Quitar" fuera del modal); revisión r1 2 CRITICAL (navegación intra-grupo no-op que scrolleaba arriba; avisos post-upload ocultos en "Detalles"); r2 sin defectos funcionales, 1 hueco de cobertura cerrado. Suite 1007→1062. Los 4 falsos positivos de lint `.detail` (bloqueaban `npm run gate`) exonerados por línea.

**Nuevo /goal del usuario (15/09)**: seguir con el mismo método; NO depender de GitHub Actions (facturación bloqueada en ambos repos, `startup_failure` en 0-5s). Decisión (engram #699 `ci/gate-sin-github-actions`): Railway NO sirve como gate de merge (solo PR environments y `preDeployCommand` de deploy, sin integración con PRs; el frontend ni deploya ahí). Gate oficial = scripts locales existentes (`scripts/pre-merge.ps1` backend, `npm run gate` frontend) + hook `pre-push` versionado en `.githooks/` (`make hooks` / `npm run hooks` → `core.hooksPath`) + línea `GATE OK <sha> … dirty=no` obligatoria en el PR template. Escape: `SKIP_GATE=1` o `--no-verify`. MERGEADO en ambos repos: backend PR #91 (master `1370ffa`; de paso arreglado bug real: `pre-merge.ps1` siempre decía `dirty=yes` porque `$null -ne ""` es true en PowerShell), frontend PR #66 (master `1ab2dd4`). `core.hooksPath=.githooks` ya instalado en ambos checkouts locales. 3.5 (Pantalla 3 "Revisá y radicá") MERGEADO (frontend PR #67, squash `db45c9f`): async-first (el job de 2.7 nunca había sido cableado al wizard), preview con datos ya recibidos y descartados, radicar intacto. r1 halló polling infinito al abrir el grupo 3 (pending sintético del backend) y umbral de stall inútil; r2 halló que un fallo de transporte tras encolar silenciaba el polling para siempre (arreglado con un refetch de confirmación). Suite 1103. 4.3 MERGEADO (backend PR #92, squash `a730acf`, suite 2832→2974): además de los ~140 tests, arregló bugs reales de mapeo (Drive filtraba `HttpError` crudo en 8 métodos → 500 en vez de 502; Calendar `get_event` y Gmail `get_attachment` sin wrapping; base64/`data:null`/JSON malformado; errores de transporte no-`OSError`; un mensaje corrupto tiraba el batch; URIs con `q=` en el detail del 502). r2 halló que r1 había clasificado un grant REVOCADO como caída transitoria → nuevo código `GOOGLE_REAUTH_REQUIRED`. **TODA la lista de tareas del plan está completa (Fases 0-4).** Quedan solo follow-ups rastreados en los cuerpos de los PRs #65/#66/#67/#91/#92 y la decisión UX del usuario sobre la altura del grupo 2. Próximo paso natural: sdd-verify del cambio completo contra tasks.md y criterios de salida, luego archivar.

**Pendiente de decisión del usuario**: grupo 2 con `revealMode:"all"` mide ~5800-7100px dentro de una banda de scroll de ~373px — colapsar miembros por defecto / acordeón / dejar así. Restante del plan: 3.5, 4.3, 4.8 (ahora sin Actions: el job de Playwright completo pasa a ser parte del gate local o queda como doc).

## Estado 2026-09-15 (noche): 3.4 IMPLEMENTADO pero sin commitear, sin PR, sin revisión (SUPERADO por la sección anterior)

Frontend en branch `feat/phase3-4-screen-2-completa-tu-cuota` (0 commits sobre master `b77fb9e`), 26 archivos modificados + `components/ui/__tests__/progress-bar.test.tsx` sin trackear, ~995 líneas. Auditoría independiente (opus, read-only, engram #698 `sdd/radicacion-sin-friccion/3.4-wip-audit`): tsc limpio, vitest 1044/1044, 0 lint nuevos (19 preexistentes en master), 0 selectores e2e rotos. Faltan SOLO validaciones: Playwright en vivo (una aserción cambió de polaridad: `step-6-justificacion` ahora visible por `revealMode:"all"`) y revisión adversarial fresca. Dos textos viejos "Formalizar" quedaron en `formato-eleccion-dialog.tsx:58` y `checklist-full-view.tsx:79`; línea 3.5 duplicada en tasks.md (L193/194). Backup del diff en el scratchpad de la sesión de1ad887 (`wip-3.4-backup.patch`). Stash viejo del 06/09 en el frontend es ajeno, no tocar.

**Gotcha nuevo**: un carácter Unicode literal tipeado directo en el código fuente (en vez de un rango `\uXXXX` escapado) puede sobrevivir silenciosamente a una edición que PARECE haber cambiado el texto pero reprodujo los mismos bytes — para regex/strings que necesitan un rango Unicode específico, escribirlo vía un script Python a nivel de bytes y verificar leyendo los bytes crudos después, no confiar en que re-tipear "arregló" algo.
