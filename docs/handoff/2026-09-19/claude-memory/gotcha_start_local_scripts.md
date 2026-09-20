---
name: gotcha-start-local-scripts
description: "RESOLVED 2026-07-17: root start-local.ps1 now delegates to cashing-backend/scripts/start-local.ps1 — one source of truth (local_dev.db + port cleanup)"
metadata: 
  node_type: memory
  type: project
  originSessionId: e8a08e3f-5ee9-4bc5-bfd9-4bb306fd350b
---

RESOLVED 2026-07-17: root `start-local.ps1` is now a thin delegator to `cashing-backend/scripts/start-local.ps1` (forwards -BackendPort/-FrontendPort/-NoFrontend, keeps the optional Ollama check). Both entry points now boot against `local_dev.db` with port cleanup. Historical context below.

Root `start-local.ps1` (old version) let the backend read `.env` → `dev.db` (9 contratos, stale). `cashing-backend/scripts/start-local.ps1` hardcodes `DATABASE_URL=sqlite+aiosqlite:///./local_dev.db` → the user's real local data (20 contratos). Same app, different DB — logging in against the wrong one looks like "contracts disappeared".

The scripts/ version also cleans ports 8000-8010/3000/3001, auto-picks a free backend port (zombie socket on 8000 → backend on 8001), and rewrites frontend `.env.local` `NEXT_PUBLIC_API_URL` to match — don't manually revert it to 8000.

Zombie reloader: matar el listener por puerto NO alcanza — el worker de `uvicorn --reload` (hijo multiprocessing) sobrevive con parent muerto y re-sirve el puerto viejo con la DB vieja. Detectarlo con `Get-CimInstance Win32_Process` (python.exe con ParentProcessId muerto y CreationDate viejo) y `Stop-Process -Force`. Verificar con `Invoke-WebRequest` que el puerto realmente murió. Tras cambiar de DB, el JWT del navegador queda inválido (UUID de usuario distinto por DB) → logout + login.

Related: [[gotcha-local-sqlite-schema]]. Other DB files: `local_smoke.db` (smoke), `cashin_dev.db` (old).
