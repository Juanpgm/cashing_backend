---
name: gotcha-agent-worktree-isolation-cwd
description: "Agent tool con isolation:\"worktree\" clona el repo del cwd ACTUAL de la sesión al momento del lanzamiento, no el repo mencionado en el prompt"
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 41a46c5d-260f-4dcd-9f01-448d2d7da506
  modified: 2026-09-12T21:21:13.170Z
---

Verificado 2026-09-12: al lanzar dos `Agent` calls con `isolation: "worktree"` en paralelo pidiendo trabajo sobre `cashing-backend-master`, uno de los dos sub-agentes arrancó en el worktree equivocado (`cashing-frontend`) porque el cwd de la sesión padre había quedado ahí tras el último `Bash` call (un `git switch master` en frontend) justo antes de invocar `Agent`. El sub-agente detectó la falta de `pyproject.toml`/`conftest.py` y reportó `status: blocked` correctamente en vez de improvisar — comportamiento correcto del lado del sub-agente.

**Why:** en un workspace con múltiples repos hermanos (backend, backend-master, frontend), `isolation: "worktree"` no lee el repo target del texto del prompt; clona lo que sea que el shell tenga como directorio actual en ese instante.

**How to apply:** antes de cualquier `Agent` call con `isolation: "worktree"`, confirmar con un `Bash` (`pwd` + `git branch --show-current`) que el cwd es el repo correcto, especialmente después de haber tocado otro repo en el mismo turno (como en [[gotcha-backend-checkout-stale]]). Si se lanzan varios `Agent` en paralelo apuntando a repos distintos, hacerlo en turnos separados con el cwd confirmado entre medio, no en el mismo bloque de tool calls.
