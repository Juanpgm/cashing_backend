---
name: neon-dev-setup
description: "Setup de Neon (proyecto cashing-prototype, org Juan) — rama dev sincronizada por create_all, fix SSL por-host, toggle local↔Neon, y hallazgo grave de migraciones"
metadata: 
  node_type: memory
  type: project
  originSessionId: e5e15c33-ec3e-406b-aa2a-224cc5045463
---

# Neon dev setup (2026-07-07)

## Proyecto / ramas
- Org **Juan** (`org-late-lab-56653882`), proyecto **cashing-prototype** (`fragrant-wave-69296517`), **PostgreSQL 16.14**, región sa-east-1. pgvector 0.8.0 disponible.
- Ramas: **`production`** (default/primary, VACÍA — no se tocó) + **`dev`** (`br-frosty-moon-acaqoxba`, hija de production, endpoint `ep-autumn-union-acgvv03i`).
- Se sincronizó SOLO `dev` (como pidió el usuario).

## Cómo se sincronizó dev (clave)
NO por alembic — por **`create_all` desde los modelos** (igual que el local SQLite). Resultado: 27 tablas idénticas al local + extensión `vector` instalada + `alembic stamp head` (021_invite_codes) para que el lifespan no falle. Local-SQLite y Neon-dev quedan gemelas.

## HALLAZGO GRAVE (pendiente para producción)
**Las migraciones alembic NO crean el schema base.** Verificado: ninguna migración crea usuarios/contratos/obligaciones/cuentas_cobro/etc.; solo 8 migraciones crean tablas periféricas (secop, google_tokens, agent, requisitos, invite_codes). El schema base SIEMPRE se construyó con `create_all`. Por eso `alembic upgrade head` en DB fresca FALLA (migración 002 hace `DELETE FROM obligaciones` y la tabla no existe). En la app real quedaba tapado porque el lifespan corría alembic (falla, se loguea) y después `create_all` construye todo. **Alembic hoy es decorativo.** Antes de confiar en migraciones en Railway hay que crear una migración baseline que capture el schema completo actual. Ver [[session_20260706_block_waitlist_semantic]].

**UPDATE 07-07 (fix parcial):** el lifespan (`app/main.py`) generaba un traceback en CADA boot local (`alembic upgrade` antes de create_all → replay de 001_secop_tables → "table secop_contratos already exists"; peor si `alembic_version` quedaba vacía tras un fallo previo, porque el DDL de SQLite autocommittea). FIX aplicado: **create_all PRIMERO**, luego alembic decide por estado real — `stamp head` si `alembic_version` no existe o está vacía / `upgrade head` si tiene fila de versión. Boot local ahora con **0 tracebacks** (verificado: 1er boot stampea, siguientes upgrade no-op). NO reemplaza la migración baseline real que sigue pendiente para prod.

## Fix de SSL (código, ya aplicado)
`app/core/db_ssl.py` nuevo: `prepare_pg_url(url)` → strip de `sslmode`/`channel_binding` (asyncpg no los parsea) + SSL por HOST (localhost→off, remoto→ssl CERT_NONE). Reemplazó la lógica vieja por-environment en `database.py` y `alembic/env.py`. Antes: `ssl:False` salvo producción → Neon fallaba desde dev. Ahora dev conecta a Neon con SSL.

## Toggle local ↔ Neon
- `start-local.ps1` (sin cambios) → SQLite local.
- `start-local-neon.ps1` (nuevo, en la raíz) → lee `DATABASE_URL_NEON_DEV_DIRECT` de `secrets/.env.local` y bootea la app contra Neon dev (endpoint DIRECTO). Backend :8000, front :3000.
- Connection strings de dev guardadas en `cashing-backend/secrets/.env.local`: `DATABASE_URL_NEON_DEV_DIRECT` y `DATABASE_URL_NEON_DEV_POOLED` (gitignorado).

## Verificado
App layer end-to-end contra Neon dev: INSERT+SELECT+DELETE de Usuario OK en directo Y pooled. 44 tests de regresión verdes tras el cambio de SSL. Migración/stamp confirmados con `alembic current` → 021_invite_codes.

## Gotchas
- Endpoint pooled de Neon = PgBouncer transacción (no soporta prepared statements). En pruebas simples anduvo, pero para runtime el toggle usa el DIRECTO por robustez.
- pgvector: `create_all` NO crea el índice ivfflat de la migración 008. La búsqueda semántica funciona en Neon (extensión instalada) pero sin índice (seq scan). Optimización futura.
- SEGURIDAD: el usuario pegó en el chat la password de la DB y el API key de Neon "cashing-prototype" → **rotar ambos** cuando termine.
- neonctl 2.30.1 instalado global. Auth por `NEON_API_KEY` (no había CLI/psql/api key antes).
