# Guía de pruebas — Swagger UI (Railway)

**URL:** https://cashin-api-production.up.railway.app/docs

---

## 0. Autenticación (hacer primero, una sola vez)

### 0.1 Registrar usuario

1. En la página de Swagger, busca la sección **auth**
2. Abre `POST /api/v1/auth/register` → click en **Try it out**
3. Reemplaza el body con:

```json
{
  "email": "tu@email.com",
  "password": "TuPassword123*",
  "nombre": "Tu Nombre"
}
```

4. Click **Execute**
5. Respuesta esperada: `201` con tu usuario y `"creditos_disponibles": 30`

---

### 0.2 Login

1. Abre `POST /api/v1/auth/login` → **Try it out**
2. Body:

```json
{
  "email": "tu@email.com",
  "password": "TuPassword123*"
}
```

3. Click **Execute**
4. En la respuesta copia el valor de `access_token` (el string largo que empieza con `eyJ...`)

---

### 0.3 Activar el token en Swagger

> **Dónde está el botón:** En la parte superior de la página `/docs`, en la misma línea del título **CashIn Backend**, a la derecha, hay un botón que dice **Authorize 🔓**.
>
> ```
> CashIn Backend  0.1.0   OAS3          [ Authorize 🔓 ]
> ```
>
> Si no aparece: recarga la página y espera a que cargue el JavaScript completamente.

1. Click en el botón **Authorize** (arriba a la derecha, antes de la lista de endpoints)
2. Se abre un modal. En el campo bajo **HTTPBearer (http, Bearer)**:
   - Pega únicamente el token (sin escribir "Bearer", Swagger lo agrega solo)
3. Click **Authorize** → click **Close**
4. El candado cambia a 🔒 (cerrado) — ya estás autenticado

> A partir de aquí todos los requests incluyen el header `Authorization: Bearer <token>` automáticamente.

---

## 1. Auth

### Ver mi perfil

`GET /api/v1/auth/me`

- Sin parámetros, sin body
- Respuesta esperada `200`:
```json
{
  "id": "...",
  "email": "tu@email.com",
  "nombre": "Tu Nombre",
  "creditos_disponibles": 30
}
```

### Actualizar perfil

`PUT /api/v1/auth/me` → **Try it out**

```json
{
  "nombre": "Nombre Actualizado",
  "cedula": "1234567890",
  "telefono": "3001234567"
}
```

### Renovar token

`POST /api/v1/auth/refresh` → **Try it out**

```json
{
  "refresh_token": "<pega el refresh_token que obtuviste en el login>"
}
```

### Cerrar sesión

`POST /api/v1/auth/logout` — Sin body. Invalida el token actual.

---

## 2. Contratos

### 2.1 Crear contrato

`POST /api/v1/contratos/` → **Try it out**

```json
{
  "numero_contrato": "CD-311-2024",
  "objeto": "Prestación de servicios profesionales de desarrollo de software",
  "valor_total": 24000000,
  "valor_mensual": 4000000,
  "fecha_inicio": "2024-01-01",
  "fecha_fin": "2024-06-30",
  "entidad": "MINISTERIO DE TECNOLOGÍAS",
  "supervisor_nombre": "Juan García",
  "dependencia": "Dirección TI",
  "obligaciones": [
    {
      "descripcion": "Desarrollar módulos del sistema según requerimientos técnicos",
      "tipo": "especifica",
      "orden": 1
    },
    {
      "descripcion": "Participar en reuniones de seguimiento semanales",
      "tipo": "general",
      "orden": 2
    }
  ]
}
```

> **Importante:** Copia el `id` de la respuesta. Lo necesitas en los pasos siguientes.  
> Ejemplo: `"id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"`

Respuesta esperada: `201`

### 2.2 Listar contratos

`GET /api/v1/contratos/` — Sin parámetros. Devuelve todos tus contratos activos.

### 2.3 Ver un contrato

`GET /api/v1/contratos/{contrato_id}`

- En el campo `contrato_id` pega el `id` del contrato creado

### 2.4 Actualizar contrato

`PATCH /api/v1/contratos/{contrato_id}` → **Try it out**

```json
{
  "supervisor_nombre": "María López",
  "valor_mensual": 4500000
}
```

Solo envía los campos que quieres cambiar.

### 2.5 Agregar obligación

`POST /api/v1/contratos/{contrato_id}/obligaciones` → **Try it out**

```json
{
  "descripcion": "Entregar informes mensuales de avance técnico",
  "tipo": "especifica",
  "orden": 3
}
```

> El `id` de la obligación aparece en la respuesta — guárdalo para eliminarlo si quieres.

### 2.6 Eliminar obligación

`DELETE /api/v1/contratos/{contrato_id}/obligaciones/{obligacion_id}`

- Pega el `contrato_id` y el `obligacion_id`
- Respuesta esperada: `204 No Content`

### 2.7 Eliminar contrato

`DELETE /api/v1/contratos/{contrato_id}`

- Respuesta esperada: `204 No Content`
- **Falla con `409`** si hay cuentas de cobro en estado `ENVIADA`, `APROBADA` o `PAGADA`

---

## 3. Cuentas de Cobro

> Necesitas un `contrato_id` válido del paso anterior.

### 3.1 Crear cuenta de cobro

`POST /api/v1/cuentas-cobro/` → **Try it out**

```json
{
  "contrato_id": "<id-del-contrato>",
  "mes": 1,
  "anio": 2024,
  "actividades": [
    {
      "descripcion": "Desarrollo del módulo de autenticación y gestión de usuarios del sistema",
      "obligacion_id": null
    },
    {
      "descripcion": "Reuniones de seguimiento con el equipo técnico (4 sesiones de 2 horas)",
      "obligacion_id": null
    }
  ]
}
```

> Copia el `id` de la cuenta de cobro de la respuesta.

Respuesta esperada: `201` con `"estado": "BORRADOR"`

### 3.2 Listar cuentas de cobro

`GET /api/v1/cuentas-cobro/`

Parámetros opcionales:
- `estado`: `BORRADOR` | `ENVIADA` | `APROBADA` | `RECHAZADA` | `PAGADA`

### 3.3 Ver cuenta de cobro

`GET /api/v1/cuentas-cobro/{cuenta_id}` — Pega el `id` de la cuenta.

### 3.4 Agregar actividad

`POST /api/v1/cuentas-cobro/{cuenta_id}/actividades` → **Try it out**

```json
{
  "descripcion": "Documentación técnica de los módulos desarrollados durante el mes",
  "obligacion_id": null
}
```

### 3.5 Cambiar estado (máquina de estados)

`PATCH /api/v1/cuentas-cobro/{cuenta_id}/estado` → **Try it out**

Flujo válido:

```
BORRADOR → ENVIADA → APROBADA → PAGADA
                   ↘ RECHAZADA → BORRADOR
```

**Enviar:**
```json
{ "estado": "ENVIADA" }
```

**Aprobar:**
```json
{ "estado": "APROBADA" }
```

**Rechazar** (desde ENVIADA):
```json
{ "estado": "RECHAZADA" }
```

**Marcar pagada** (desde APROBADA):
```json
{ "estado": "PAGADA" }
```

### 3.6 Generar PDF

`POST /api/v1/cuentas-cobro/{cuenta_id}/generar-pdf`

- Sin body
- Genera el PDF y lo sube a S3
- Respuesta: URL de descarga

### 3.7 Obtener URL del PDF

`GET /api/v1/cuentas-cobro/{cuenta_id}/pdf`

- Devuelve una URL presignada de S3 válida por 1 hora
- Puedes pegar esa URL en el navegador para descargar el PDF

---

## 4. SECOP — Contratos públicos (datos.gov.co)

> Los primeros requests consultan la API de SECOP (~4s). Los siguientes usan cache local y responden en <1s.

### 4.1 Buscar contratos por cédula

`GET /api/v1/secop/contratos` → **Try it out**

Parámetros:
- `cedula`: `1016019452` ← cédula real de prueba con contratos en SECOP
- `refresh`: `false` (dejar por defecto)

Respuesta esperada: lista de contratos de **"Prestación de servicios"** con campos como:
- `id_contrato_secop`, `nombre_entidad`, `valor_del_contrato`
- `proceso_de_compra` ← úsalo en el siguiente endpoint
- `estado_contrato`, `fecha_inicio`, `fecha_fin`

### 4.2 Ver proceso de contratación

`GET /api/v1/secop/procesos/{id_proceso}` → **Try it out**

- `id_proceso`: pega el valor de `proceso_de_compra` del paso anterior
- Ejemplo: `CO1.BDOS.6724470`

Respuesta: detalles del proceso (entidad, modalidad, precio base, estado, fechas).

### 4.3 Ver documentos de un contrato

`GET /api/v1/secop/documentos/{numero_contrato}` → **Try it out**

- `numero_contrato`: `CO1.PCCNTR.9005900` ← número de prueba conocido

Respuesta: lista de archivos con `url_descarga` para bajar PDFs directamente desde SECOP.

### 4.4 Consulta completa integrada

`GET /api/v1/secop/consulta` → **Try it out**

Parámetros:
- `cedula`: `1016019452`
- `refresh`: `false`

Respuesta: un solo objeto con todos los contratos + el proceso asociado a cada uno + sus documentos.

```json
{
  "cedula": "1016019452",
  "total_contratos": 17,
  "contratos": [
    {
      "contrato": { ... },
      "proceso": { ... },
      "documentos": [ ... ]
    }
  ]
}
```

---

## 5. Chat con el agente IA

> Cada mensaje consume **1 crédito**. Verifica tu saldo en `GET /auth/me`.

### 5.1 Iniciar conversación

`POST /api/v1/chat/` → **Try it out**

```json
{
  "mensaje": "Hola, tengo un contrato con el Ministerio de Educación por 3 meses. ¿Me ayudas a crear una cuenta de cobro?"
}
```

> Copia el `session_id` de la respuesta.

### 5.2 Continuar conversación

`POST /api/v1/chat/` → **Try it out**

```json
{
  "session_id": "<id-de-la-sesion>",
  "mensaje": "El valor mensual es 4 millones y empecé en enero de 2024"
}
```

### 5.3 Ver historial de la sesión

`GET /api/v1/chat/{session_id}` — Pega el `session_id`.

---

## 6. Documentos

### 6.1 Subir archivo

`POST /api/v1/documentos/upload` → **Try it out**

- Haz click en **Choose File** y selecciona un PDF o DOCX de un contrato
- Click **Execute**
- Copia el `id` del documento de la respuesta

### 6.2 Procesar con IA

`POST /api/v1/documentos/process` → **Try it out**

```json
{
  "documento_id": "<id-del-documento-subido>"
}
```

El agente extrae automáticamente: número de contrato, entidad, valor, fechas y obligaciones.

---

## Errores comunes

| Código | Causa | Solución |
|--------|-------|----------|
| `401` | Token expirado o no autorizado | Repetir login y re-autorizar en Swagger |
| `402` | Sin créditos suficientes | Ver saldo en `GET /auth/me` |
| `404` | ID no existe o no te pertenece | Verificar el `id` usado |
| `409` | Conflicto de estado | Ej: cuenta en ENVIADA no se puede borrar |
| `422` | Body inválido | Revisar tipos y campos requeridos |
| `502` | API de SECOP no disponible | Reintentar en unos minutos |

---

## Orden recomendado para una prueba completa

```
1. register → login → Authorize
2. POST /contratos        → guarda contrato_id
3. POST /cuentas-cobro    → guarda cuenta_id
4. PATCH /cuentas-cobro/{id}/estado → "ENVIADA"
5. POST /cuentas-cobro/{id}/generar-pdf
6. GET /cuentas-cobro/{id}/pdf       → descarga el PDF
7. GET /secop/consulta?cedula=1016019452
8. POST /chat/ con una pregunta sobre tu contrato
```
