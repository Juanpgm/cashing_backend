---
name: feedback-local-first
description: Regla del usuario — probar TODO en local antes de cualquier deploy (Vercel/Railway/push que dispare builds)
metadata: 
  node_type: memory
  type: feedback
  originSessionId: 358c2511-38c3-47f0-9b7a-26b5a9846567
---

2026-07-10: el usuario interrumpió el pipeline de deploy (Vercel + Railway) con: "debes probar primero todo en local solamente".

**Why:** Quiere ver el producto funcionando end-to-end en local antes de exponer nada, aunque el plan aprobado incluyera deploy.

**How to apply:** Orden siempre: suite completa → arranque local (backend :8000 + frontend :3000, `scripts/start-local.ps1`) → smoke manual → pedir confirmación explícita para deployar. Nunca `railway up`, `vercel deploy` ni pushes que disparen builds sin ese OK en la sesión. Contexto pendiente de deploy en [[git-baseline-cashin]] y el session summary de Engram (2026-07-10).
