---
name: gotcha-backend-checkout-stale
description: El checkout cashing-backend/ está en la rama vieja integracion-stepper-local (215 commits detrás de master); el código real vive en cashing-backend-master/
metadata: 
  node_type: memory
  type: project
  originSessionId: 41a46c5d-260f-4dcd-9f01-448d2d7da506
  modified: 2026-09-12T05:42:37.750Z
---

Verificado 2026-09-12: `cashing-backend/` (worktree principal) está en `integracion-stepper-local` @ 8a5949e, **215 commits detrás de origin/master** y con 15 archivos modificados sin commitear (checklist, cuenta_cobro, radicacion_prep, alembic 026/028) que en su mayoría YA están resueltos en master. El worktree `cashing-backend-master/` está en `master` @ 7d33352 (= producción v0.2.1). Otros worktrees: `-prod` (feat/agente-flujo-e2e), `-prodfix` (fix/groq-fallback-model-decommissioned), `-hangdiag` (mergeado, borrable).

**Why:** cualquier auditoría o edición hecha en `cashing-backend/` opera sobre código obsoleto; las memorias de julio referencian esa rama pero la canónica cambió en septiembre. Ver [[bug-web-borra-contrato]] y [[git-baseline-cashin]].

**How to apply:** trabajar y delegar SIEMPRE sobre `cashing-backend-master/` (o una rama nueva desde master). Antes de tocar `cashing-backend/`, decidir con el usuario si el diff sin commitear se descarta o se rescata (no usar `git checkout <ref> -- .`, ver [[feedback-git-safety-subagents]]).
