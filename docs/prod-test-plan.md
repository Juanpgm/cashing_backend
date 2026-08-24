# Plan de pruebas de producción — cashing

Runbook para validar el stack en vivo sin arriesgar los datos reales. La regla de
oro: **nada que escriba datos de negocio corre contra producción**. Producción
solo se toca en modo lectura; todo lo destructivo corre contra el stack local
(que aquí llamamos *staging*).

---

## Estrategia de 3 capas

| Capa | Objetivo | Dónde corre | Escribe datos de negocio |
|------|----------|-------------|--------------------------|
| **1. Smoke de producción (solo lectura)** | Confirmar que el stack vivo responde tras un deploy de Railway (health, auth, lecturas). | Prod (`cashin-api-production.up.railway.app`) | No |
| **2. E2E completo (staging = stack local)** | Ejercer el flujo destructivo real: radicación, evidencias, paquete, SECOP, pagos. | Local (`localhost:8000` + `localhost:3000`) con una **DB de e2e dedicada** | Sí (por eso NO es prod) |
| **3. Checklist manual** | Lo que no corre headless: OAuth real, popups de Firebase, sandbox de pagos, CORS en navegador real. | Prod / staging con un humano | Depende del paso |

- **Capa 1** es la única que apunta a producción, y es de solo lectura por diseño
  (`scripts/smoke_prod.py`: solo GETs + un login que no crea datos).
- **Capa 2** usa una base de datos desechable porque los specs de Playwright
  **auto-siembran** usuarios, contratos y cuentas de cobro (register + login por
  API). Correrlos contra prod ensuciaría los datos reales — por eso staging = local.
- **Capa 3** cubre integraciones con terceros (Google, Wompi) que exigen un
  navegador real y un humano que apruebe popups.

---

## Cómo correr cada capa

### Capa 1 — Smoke de producción (solo lectura)

Requiere una cuenta de prueba dedicada ya existente en prod (nunca hardcodear
credenciales). Sin `SMOKE_USER`/`SMOKE_PASS` corre solo los health checks
anónimos y omite las lecturas autenticadas con un aviso.

Desde `cashing-backend/`, en PowerShell (Windows):

```powershell
$env:SMOKE_BASE_URL = "https://cashin-api-production.up.railway.app"
$env:SMOKE_USER = "prod-test@tu-dominio.com"
$env:SMOKE_PASS = "<password de la cuenta de prueba>"
# opcional — cédula usada para la consulta SECOP de solo lectura (default 1016019452)
# $env:SMOKE_CEDULA = "1016019452"
uv run python scripts/smoke_prod.py
```

En bash/POSIX:

```bash
SMOKE_USER=prod-test@tu-dominio.com SMOKE_PASS=... uv run python scripts/smoke_prod.py
```

Qué valida, en cadena (cada paso imprime `PASS` / `FAIL` / `WARN` / `SKIP`):

1. `GET /health` → 200 con `environment`.
2. `GET /api/v1/health/llm` → 200 con `status` en `{ok, degraded}`. Un `error` o
   una caída del proveedor LLM es **WARN**, no falla el run.
3. `POST /api/v1/auth/login` → 200, extrae el `access_token`.
4. Lecturas autenticadas (solo GET): `/cuentas-cobro/`, `/contratos/`,
   `/dashboard`, `/creditos/balance`, `/secop/consulta?cedula=...`. La consulta
   SECOP consume la API externa de datos.gov.co, así que un 5xx aguas arriba es
   **WARN**, no `FAIL`.

Código de salida `0` si todos los pasos que no son WARN pasan; `1` en caso contrario.

### Capa 2 — E2E completo (staging = stack local)

Levantar el backend local (con una DB de e2e dedicada) y el frontend, y correr
Playwright. Desde `cashing-frontend/`:

```bash
npm run test:e2e:staging
```

`test:e2e:staging` fija `NEXT_PUBLIC_API_URL=http://localhost:8000` (el stack
local) antes de invocar Playwright — así, aunque tu shell tenga esa variable
apuntando a otro lado (p. ej. a prod), los E2E destructivos nunca corren contra
producción por accidente.

Specs relevantes:
- `e2e/radicacion-paquete-completo.spec.ts` — flujo completo del wizard +
  descarga real del ZIP + descargables individuales (docx/xlsx/zip).
- `e2e/secop-flujo.spec.ts` — superficie SECOP del step 1 (tolerante a upstream).
- `e2e/dashboard-creditos.spec.ts` — dashboard + créditos + cobertura (lectura).

Suite de backend (nunca `uv run pytest` a secas — segfault de libmagic en
Windows + py3.12). Desde `cashing-backend/`:

```bash
uv run python -m pytest
```

Gate completo antes de commit (desde `cashing-backend/`):

```bash
make format && make lint && make test
```

### Capa 3 — Checklist manual

Ver la sección siguiente.

---

## Checklist manual (lo que no corre headless)

Estos flujos requieren un navegador real y un humano. Marcar cada uno tras verificarlo.

- **Google OAuth real (login).** Firebase `signInWithPopup` → `POST /api/v1/auth/google`
  con el ID token. Verificar que el popup abre, que la cuenta se crea/actualiza y
  que vuelve el par de JWT. No es automatizable: el popup de Google bloquea a headless.
- **Descubrimiento de evidencias con Google (step 6).** En el step de
  justificaciones, la acción de "Generar justificaciones" intenta descubrir
  evidencias vía Gmail/Drive/Calendar. Sin proveedor conectado responde
  `NO_PROVIDER_CONNECTED` (banner `step-6-no-provider`) — comportamiento
  esperado. Con Google conectado, verificar que trae adjuntos reales.
- **Integraciones Google (`/integraciones`).** Flujo de conexión OAuth:
  `GET /integraciones/google/connect` → conceder scopes → callback. Verificar que
  los tokens quedan cifrados (Fernet) y que las herramientas MCP funcionan.
- **Pagos Wompi (sandbox).** `POST /api/v1/pagos/checkout` devuelve la URL de
  checkout de Wompi; completar un pago en el sandbox y confirmar que el webhook
  (`/api/v1/webhooks/...`) acredita los créditos. Usar SIEMPRE llaves de sandbox,
  nunca las de producción.
- **CORS / COOP en navegador real.** Confirmar que `Cross-Origin-Opener-Policy`
  es `unsafe-none` (lo exige el popup de Firebase; una política más estricta lo
  rompe) y que `CORS_ORIGINS` incluye el origen de Vercel del frontend. Verificar
  en la consola del navegador que no hay errores de CORS ni de COOP al hacer login
  con Google desde el dominio de prod.

---

## Diferencias prod ↔ staging a vigilar

Un test que pasa en staging puede fallar en prod por diferencias de entorno.
Vigilar:

- **Almacenamiento.** Prod usa `STORAGE_PROVIDER` S3/R2 (Cloudflare) o el Volume
  de Railway; staging local usa el adapter local / MinIO. Rutas, permisos y URLs
  presignadas difieren — el smoke de prod NO descarga archivos precisamente para
  no depender de esto.
- **CORS.** En prod `CORS_ORIGINS` debe incluir el origen exacto de Vercel; en
  local suele ser `localhost:3000`. Un origen faltante rompe el login desde el
  frontend desplegado.
- **Gates de arranque.** En prod están activos `COHERENCE` y `SECRET_SCAN`; en
  local pueden estar relajados. Un secreto mal configurado que en local se ignora,
  en prod frena el boot.
- **Feature switches.** El scraper agéntico de SECOP (`SECOP_SCRAPER_ENABLED`) y
  otros switches suelen estar **apagados** por defecto — por eso los specs de
  SECOP toleran datos externos vacíos o un 5xx aguas arriba en vez de fallar.
- **Panel de debug.** Las rutas `/api/v1/debug/*` solo se montan en entornos no
  productivos (`development`/`dev`/`local`/`test`) — no asumir su existencia en prod.

---

## Gate "de que todo sirve" (criterio de aceptación)

Se considera el stack sano para producción cuando:

1. **Smoke de prod** sale con código `0` (health, LLM al menos `degraded`, login y
   las cinco lecturas autenticadas en 2xx; SECOP en 2xx o WARN por upstream).
2. **E2E de staging** (`npm run test:e2e:staging`) verde: el flujo de radicación
   llega al ZIP real y los descargables individuales devuelven 2xx con su
   `Content-Type` + `Content-Disposition` correctos.
3. **Suite de backend** (`uv run python -m pytest`) verde y `make format && make
   lint` limpios sobre los archivos tocados.
4. **Checklist manual** completo para lo que toque el cambio (OAuth, pagos, CORS/COOP).

Si las cuatro condiciones se cumplen, el cambio está listo para producción.
