---
name: feedback-testing
description: Convenciones de testing y comandos correctos para este proyecto
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 8c6ea991-1202-450e-9fb1-00114a1bdb30
---

Usar `uv run python -m pytest` en lugar de `uv run pytest` para correr tests.

**Why:** El script de consola `pytest` no resuelve correctamente en el venv de este proyecto, pero el módulo Python sí importa. Descubierto por error al intentar `uv run pytest` que devolvía "program not found".

**How to apply:** Siempre que se necesite correr tests en `cashing-backend`, usar `uv run python -m pytest tests/... -v`. Aplica también para `uv run python -m pytest` con flags como `-k`, `--cov`, etc.
