---
name: feedback-git-safety-subagents
description: "Regla dura tras incidente 18/07 — subagentes destruyeron un edit sin commitear con `git checkout <ref> -- .`; protocolo de respaldo obligatorio antes de operaciones git"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: b3cd08e0-606a-4e2b-977a-0850cd8e3a5c
---

Incidente 2026-07-18: un sub-agente ejecutó `git checkout master -- .` en cashing-frontend para "cambiar de rama" y sobrescribió la modificación sin commitear de `contrato-documentos.tsx` (el fix SECOP del 11/07 nunca commiteado). Irrecuperable por git/fsck/VS Code history/sourcemaps.

**Why:** `git checkout <ref> -- <paths>` restaura archivos desde el ref pisando el working tree; no es un branch-switch y no preserva edits locales.

**How to apply:**
- En prompts de delegación que involucren git, incluir SIEMPRE: prohibido `git checkout <ref> -- .` / `git restore --source`; branch-switch solo con `git switch <branch>` o `git checkout <branch>` sin paths.
- Antes de que un agente toque un repo con cambios sin commitear: respaldar primero (`git stash push -u` + `git stash apply` inmediato, o copiar los archivos modificados al scratchpad) y recién después operar.
- Si el orquestador detecta edits sin commitear valiosos con >1 día (como ese fix), proponer commitearlos ANTES de delegar trabajo sobre ese repo.

Relacionado: [[gotcha-local-sqlite-schema]] (otro caso de estado local frágil frente a cambios de rama).
