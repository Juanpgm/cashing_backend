---
name: feedback-tdd-proporcional
description: "Desde 19/09 el usuario pidió TDD menos estricto: RED/mutación/revisión r2 solo donde el riesgo lo justifica; casos borde siguen siendo obligatorios"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: f5ec3edf-f437-4244-8541-ed31d1cb8849
  modified: 2026-09-20T03:01:23.761Z
---

El usuario pidió (2026-09-19): "no hagas el TDD tan estricto". Hasta entonces el método era RED-first + prueba por mutación + revisión adversarial r1 y r2 en TODO (incluso tests, docs y scripts), lo que dejó rondas largas y costosas.

**Why:** el ritual completo en cambios de bajo riesgo (tests, docs, tooling, comentarios) alargó las entregas sin cambiar el resultado; el usuario quiere avance más ágil.

**How to apply — "TDD proporcional":**
- RED-first + mutación + revisión adversarial SOLO en rutas críticas: dinero/créditos, concurrencia, pérdida de datos, migraciones que corren en producción, seguridad/credenciales, máquina de estados de radicación.
- En el resto (tests e2e, docs, scripts, refactors pequeños, correcciones de UX): escribir el test junto al código (antes o después), sin prueba por mutación, gate local verde y como mucho UNA ronda de revisión; sin r2 salvo que la revisión r1 halle un CRITICAL.
- Se mantiene la regla vigente del usuario ([[user-override-testing-standard]] en CLAUDE.md global): los casos borde (límites, vacío/null/malformado, concurrencia, fallos de red/servidor, transiciones inválidas) SIEMPRE se escriben y quedan en verde; no se acepta una suite solo de camino feliz.
- El gate local (`scripts/pre-merge.ps1`, `npm run gate`) sigue siendo obligatorio antes de push/merge. Sigue vigente [[feedback-local-first]] y pedir OK explícito antes de desplegar o escribir en producción.
- Al delegar, decirlo en el prompt del subagente ("TDD proporcional: sin mutación ni r2 salvo ruta crítica") en vez de heredar el modo estricto por defecto de `sdd-init`.
