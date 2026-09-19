# Runbook de deploy — cashin-api (backend)

Procedimiento para desplegar el backend a producción y comprobar que el deploy
realmente quedó sano. Complementa `docs/prod-test-plan.md` (que valida el stack
ya desplegado) y la sección *Deployment* de `CLAUDE.md`.

**Regla de oro:** un deploy "verde" no prueba nada. El arranque ejecuta las
migraciones de Alembic como subproceso y, si fallan, solo registra un warning:
el contenedor sigue arriba con el esquema a medias. Por eso este runbook termina
siempre con la verificación de `alembic_version` y de los datos, no con "el
deploy pasó".

---

## Cómo funciona el deploy (hechos verificados el 2026-09-19)

- El backend es el servicio `cashin-api` del proyecto Railway `cashing_backend`,
  entorno `production`.
- El deploy es **manual**: `railway up` desde `cashing-backend-master`. **No hay
  auto-deploy desde GitHub** y no existe un job de deploy (GitHub Actions está
  bloqueado por facturación). Hacer merge a `master` no despliega nada.
- El frontend (Vercel) sí despliega solo desde `master`. No lo cubre este runbook.
- Secuencia de arranque (`app.main.lifespan`): `Base.metadata.create_all` **primero**
  y luego `alembic upgrade head` (o `alembic stamp head` si `alembic_version` está
  vacía). Un fallo de Alembic es solo el warning `alembic_failed`; el éxito emite
  `alembic_ok`.
- Los deployments anteriores pasan a `REMOVED`: **no existe rollback por
  "redeploy"**. Ver la sección *Rollback*.
- `/health` devuelve una versión constante (`0.2.1`), así que **no sirve** para
  saber qué código está corriendo.

---

## Reglas que no se negocian

- **Nunca** ejecutar `railway variables --kv`: vuelca todos los secretos a la
  terminal (y al historial/logs). Para saber si una variable existe, sin ver su
  valor:

  ```powershell
  railway run --service cashin-api -- uv run python -c "import os; print('NOMBRE_VARIABLE' in os.environ)"
  ```

- Nada de secretos, DSN ni tokens en commits, mensajes de deploy (`-m`), issues o
  chats. Los nombres de proyecto y servicio sí se pueden citar.
- Toda escritura contra la base de producción (`stamp`, `UPDATE` manual) requiere
  aprobación explícita del responsable, en el momento, para ese comando exacto.
- Las migraciones deben ser idempotentes (helpers de `app/core/migration_helpers.py`);
  ver `CLAUDE.md`. Una migración que haga `create_table`/`create_index`/`add_column`
  a pelo repite el incidente de la 042.

---

## 1. Pre-checks (antes de subir nada)

Desde `cashing-backend-master` (**no** `cashing-backend/`, que está obsoleto), en
PowerShell:

```powershell
git status --short
git branch --show-current
git rev-parse --short HEAD
.\scripts\pre-merge.ps1
railway whoami
railway status
```

Criterios:

1. `git status --short` sin salida (árbol limpio) y el SHA es exactamente el que
   se quiere desplegar. Anotarlo: se usa en el mensaje de deploy y en el rollback.
2. `pre-merge.ps1` imprime la línea `GATE OK <sha> ... dirty=no` con ese mismo SHA.
   Si imprime `GATE FAIL`, no se despliega.
3. `railway whoami` responde con la cuenta esperada.
4. `railway status` muestra proyecto `cashing_backend`, entorno `production` y
   servicio `cashin-api`. Si algo difiere, detenerse y re-vincular el directorio
   (`railway link`) antes de continuar.

---

## 2. Estado de la base ANTES de desplegar (solo lectura)

`scripts/check_alembic_state.py` solo hace `SELECT`, dentro de una transacción
`SET TRANSACTION READ ONLY` (el propio servidor rechaza cualquier escritura), y
**nunca imprime el DSN**: ante un fallo solo muestra el nombre de la excepción.

```powershell
railway run --service cashin-api -- uv run python scripts/check_alembic_state.py
```

Imprime: `alembic_version` de la base, la `head` del repositorio, si están al
día, si existen la tabla `paquete_job` y su índice, y el flag
`solo_primera_cuenta` de cada fila de `requisitos_documento`.

Código de salida: `0` = la base está en la `head` del repo, `2` = atrasada o sin
versionar, `1` = no pudo leer la base.

> `railway run` ejecuta el comando **en tu máquina** con las variables del
> servicio. Por eso `DATABASE_URL` debe tener un host **público**. Si apunta a
> `*.railway.internal` no resuelve desde fuera de Railway y el script terminará
> con `check failed: <NombreDeExcepcion>`; en ese caso hay que usar la URL pública
> del proxy TCP de la base, cargada como variable de entorno local, sin pegarla
> nunca en un comando ni en un chat.

### Qué hacer según el resultado

| `alembic_version` en prod | Situación | Acción |
|---------------------------|-----------|--------|
| `042_paquete_job` | Solo falta la 043 | Desplegar normal. La 043 corre en el arranque. |
| `< 042` y la tabla `paquete_job` **ya existe** (la creó `create_all`) | La 042 antigua abortaba con `DuplicateTable` y dejaba la versión atrás | Desde el commit que hace idempotente la 042, **basta desplegar**: `upgrade head` ya no colisiona y avanza sola. Ya no hace falta `stamp` para tablas creadas por `create_all`. Solo se haría un `stamp` manual, con aprobación explícita, si el fallo viene de otra migración histórica no idempotente y se comprobó a mano que el esquema real coincide con esa revisión. |
| Vacía / sin tabla `alembic_version` | El arranque hará `stamp head` y **se salta las migraciones de datos** | Tras el deploy, aplicar a mano, con aprobación, el `UPDATE` equivalente a la 043 (ver abajo) y verificar con el script. |
| Ya en `043_checklist_primera_cuota_flags` (o la `head` actual) | Al día | Desplegar normal. |

`UPDATE` manual equivalente a la migración 043 (solo con aprobación, idealmente
dentro de una transacción explícita y verificando el conteo afectado):

```sql
UPDATE requisitos_documento
SET solo_primera_cuenta = TRUE
WHERE codigo IN ('RPC', 'CDP', 'CONTRATO');
```

Resultado esperado: `RPC`, `CDP`, `CONTRATO` en `True`; `CEDULA` y `RUT` ya
estaban en `True` y no se tocan.

---

## 3. Variables de entorno (solo si el release las necesita)

Si el release añade o cambia variables, se fijan **sin disparar un deploy** y
luego se hace un único `railway up`:

```powershell
railway variables --service cashin-api --set K=V --skip-deploys
```

Sin `--skip-deploys`, cada `--set` provoca su propio deploy con el código
anterior. Recordar que `K=V` queda en el historial de la shell; para valores
sensibles, limpiar el historial o fijarlos desde el panel de Railway.

---

## 4. Desplegar

```powershell
railway up --service cashin-api --detach -m "<mensaje corto: SHA + qué cambia>"
```

Sin secretos en el mensaje. `--detach` devuelve el control de inmediato; el
seguimiento se hace a mano:

```powershell
railway deployment list --service cashin-api
```

Repetir hasta que el deployment nuevo pase de `BUILDING`/`DEPLOYING` a
`SUCCESS` (o `FAILED`, en cuyo caso ver el rollback). Un `SUCCESS` aquí **solo**
significa que el contenedor arrancó.

---

## 5. Verificación posterior (obligatoria)

1. **Log de Alembic.** Buscar en los logs del deployment nuevo la línea
   `alembic_ok` (con `action=upgrade`, o `action=stamp` si la base estaba vacía):

   ```powershell
   railway logs --service cashin-api
   ```

   Cortar con Ctrl+C. Si aparece `alembic_failed`, el deploy **no** está sano
   aunque Railway diga `SUCCESS`: leer el `stderr` de esa línea, no reintentar a
   ciegas.

2. **Versión y datos en la base** (solo lectura, mismo script de la sección 2):

   ```powershell
   railway run --service cashin-api -- uv run python scripts/check_alembic_state.py
   ```

   Debe salir con código `0`, `alembic_version` igual a la `head` del repo y, si
   el release incluía una migración de datos, los valores esperados (para la 043:
   `RPC`, `CDP`, `CONTRATO` en `True`).

3. **Huella del código (OpenAPI).** Como `/health` no cambia de versión, elegir
   un campo o una ruta **introducidos por este release** y comprobar que está en
   el esquema vivo:

   ```powershell
   curl.exe -s https://cashin-api-production.up.railway.app/openapi.json | rg "<campo_o_ruta_del_release>"
   ```

   Sin coincidencias = el contenedor sigue con el código anterior.

4. **Salud básica:**

   ```powershell
   curl.exe -s https://cashin-api-production.up.railway.app/health
   curl.exe -s https://cashin-api-production.up.railway.app/api/v1/health/llm
   ```

   `/health` debe responder 200. `/api/v1/health/llm` debe dar `status` `ok` o
   `degraded` (una caída del proveedor LLM es WARN, no bloquea).

5. **Smoke de producción (solo lectura):** ver "Capa 1" de
   `docs/prod-test-plan.md`.

   ```powershell
   $env:SMOKE_BASE_URL = "https://cashin-api-production.up.railway.app"
   $env:SMOKE_USER = "<cuenta de prueba dedicada>"
   $env:SMOKE_PASS = "<su password, solo en esta sesión>"
   uv run python scripts/smoke_prod.py
   ```

El deploy se da por bueno solo si los cinco puntos pasan.

---

## 6. Rollback

No hay botón de "redeploy" del deployment anterior (quedó `REMOVED`). Se
reconstruye el código anterior desde un worktree y se sube como un deploy nuevo:

```powershell
git worktree add ..\cashing-backend-rollback <sha-anterior>
cd ..\cashing-backend-rollback
railway link
railway up --service cashin-api --detach -m "rollback a <sha-anterior>"
```

- En `railway link` elegir proyecto `cashing_backend`, entorno `production`
  (el worktree es un directorio nuevo y no hereda el vínculo).
- Después, repetir toda la sección 5 sobre el código restaurado.
- **El rollback de código no revierte la base.** Alembic solo avanza en el
  arranque; las columnas/tablas/datos que dejó la migración nueva permanecen. Las
  migraciones de este repo son aditivas o idempotentes, así que el código
  anterior las tolera; revertir una migración de datos (p. ej. la 043) es una
  decisión aparte y requiere aprobación explícita.
- Al terminar, retirar el worktree: `git worktree remove ..\cashing-backend-rollback`.

---

## Apéndice — qué falló el 2026-09-19 y por qué no debería repetirse

`create_all` corre antes de Alembic, así que la migración `042_paquete_job`
(un `op.create_table` a pelo) chocaba con la tabla que `create_all` ya había
creado (`DuplicateTable`). `upgrade head` abortaba, `alembic_version` se quedaba
en 041, la 043 (un `UPDATE` de datos) nunca corría y el deploy parecía verde.
Producción se reparó a mano con un `stamp` 041→042. Correcciones:

- `042` ahora crea la tabla y el índice solo si faltan.
- Helpers reutilizables en `app/core/migration_helpers.py`.
- `tests/test_migration_convention_guard.py` falla si una migración posterior a la
  043 llama a `op.create_table`/`op.create_index`/`op.add_column` directamente.
- Este runbook y `scripts/check_alembic_state.py` convierten "confirmar
  `alembic_ok` y la versión" en un paso obligatorio.
