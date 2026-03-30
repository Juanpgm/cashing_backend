# CashIn Backend — Playbooks por Tipo de Tarea (AGENTS)

> Cada agente define un playbook táctico: objetivo, entradas, reglas y criterios de salida.
> **MVP en Railway** — sin dependencia de cloud providers.
> Arquitectura Ports & Adapters permite migración futura transparente.

---

## Prioridad de Contexto

1. `INSTRUCTIONS.md` — Arquitectura, reglas, roadmap
2. `SKILLS.md` — Competencias técnicas
3. `TOOLS.md` — Stack de desarrollo y deploy
4. `AGENTS.md` — Playbooks tácticos (este archivo)

Si hay conflicto, prevalece el orden anterior.

---

## Agente: API Contracts

### Objetivo

Diseñar y mantener endpoints FastAPI consistentes, tipados y versionados bajo `/api/v1`.

### Entradas esperadas

- Caso de uso funcional
- Rol/autorización requerida
- Payload de request/response esperado

### Reglas de ejecución

1. Definir/actualizar schemas Pydantic v2 en `app/schemas/` primero
2. Implementar endpoint en `app/api/v1/`
3. Delegar toda lógica al service correspondiente — **nunca SQL en routers**
4. Inyectar dependencias via `app/api/deps.py` (`get_db`, `get_current_user`, `get_storage`, `get_llm`)
5. Usar códigos HTTP correctos: `200`, `201`, `202`, `204`, `400`, `401`, `403`, `404`, `409`, `422`
6. Rate limiting via `slowapi` en endpoints públicos

### Criterios de salida

- Endpoint documentado automáticamente en OpenAPI (`/docs`)
- Sin lógica de negocio ni SQL en router
- Tests de integración con `httpx.AsyncClient`

---

## Agente: Domain Services

### Objetivo

Implementar reglas de negocio en `app/services/` con transacciones claras y errores de dominio.

### Reglas de ejecución

1. Un caso de uso por método de servicio
2. Transacción explícita cuando haya múltiples operaciones de escritura
3. Validar reglas de negocio **antes** de persistencia
4. Lanzar excepciones de `app/core/exceptions.py` — nunca `HTTPException` directamente
5. Recibir `AsyncSession` como parámetro — no crear sesiones internas
6. Interactuar con storage/LLM solo via `Port` protocols inyectados

### Criterios de salida

- Services puros de negocio, testeables sin servidor HTTP
- Cobertura de pruebas unitarias para reglas principales
- Sin imports de `fastapi`, `boto3`, ni SDKs cloud

---

## Agente: Data and Migrations

### Objetivo

Evolucionar modelo de datos y migraciones Alembic sin romper compatibilidad.

### Reglas de ejecución

1. Cambios de modelo en `app/models/` — heredar siempre de `Base` con mixins (`UUIDMixin`, `TimestampMixin`, `SoftDeleteMixin`)
2. Generar migración: `make migration msg="descripcion_concisa"`
3. Revisar migración generada manualmente antes de commit
4. Evitar migraciones destructivas sin plan de rollback
5. Crear índices para columnas usadas en filtros frecuentes: `usuario_id`, `estado`, `mes`, `anio`, `contrato_id`
6. Registrar modelo en `app/models/__init__.py`

### Criterios de salida

- `alembic upgrade head` exitoso en local con PostgreSQL
- Migración legible, reversible y alineada al modelo
- Tests pasan con SQLite in-memory (aiosqlite)

---

## Agente: Auth and Security

### Objetivo

Garantizar autenticación JWT custom robusta y protección de endpoints por rol.

### Reglas de ejecución

1. **JWT custom con python-jose (HS256)** — NO usar JWKS, Cognito, Firebase ni proveedores externos
2. Access token: 15min, Refresh token: 7 días (configurable en `core/config.py`)
3. Validar `exp`, `sub` (UUID del usuario), `type` (access/refresh) en cada request
4. Passwords: bcrypt con cost factor 12 (`passlib[bcrypt]`)
5. Token blacklist en PostgreSQL (`TokenBlacklist` model) para logout/revocación
6. Aplicar `get_current_user` y/o `require_role` como dependencies en endpoints protegidos
7. Nunca hardcodear secretos — todo en variables de entorno
8. Encriptar tokens OAuth de terceros con Fernet (`TOKEN_ENCRYPTION_KEY`)

### Criterios de salida

- Endpoints protegidos correctamente por rol
- Errores uniformes: `401 Unauthorized`, `403 Forbidden`
- Tests para login, registro, refresh, logout, token expirado, token blacklisted

---

## Agente: Files and Evidence

### Objetivo

Gestionar evidencias y PDFs con storage S3-compatible desacoplado via `StoragePort`.

### Reglas de ejecución

1. Usar `StoragePort` protocol — **nunca importar boto3 directamente**
2. Presigned URLs para upload/download (expiración configurable)
3. Validar archivos: MIME type, extensión, tamaño máximo, magic bytes (`app/core/file_validation.py`)
4. Verificar ownership antes de cualquier operación
5. Convención de llaves: `{tipo}/{user_id}/{uuid}.{ext}` (e.g., `evidencias/{user_id}/{uuid}.pdf`)
6. Backends soportados: MinIO (dev), Cloudflare R2 (prod) — misma interfaz S3

### Criterios de salida

- Flujo completo: `presigned-upload → confirmar → listar → presigned-download`
- Tests con `moto[s3]` (mock de protocolo S3, no de AWS)
- Sin referencia directa a AWS/GCP/Azure en el código

---

## Agente: Payments

### Objetivo

Implementar cobros con consistencia financiera e idempotencia de eventos vía Wompi (Colombia).

### Reglas de ejecución

1. `Decimal` para todos los montos — nunca `float`
2. Verificar firma HMAC del webhook Wompi antes de procesar
3. Idempotencia por `referencia_externa` y/o `event_id`
4. Máquina de estados para pago: `PENDIENTE → APROBADO → FALLIDO / RECHAZADO`
5. Al aprobar pago → acreditar créditos al usuario
6. Registrar auditoría de cada transacción

### Criterios de salida

- Webhook seguro y repetible sin duplicar efectos
- Tests para firma válida/inválida, pagos duplicados, estados inválidos

---

## Agente: Admin and Reporting

### Objetivo

Entregar endpoints administrativos eficientes y seguros para alto volumen.

### Reglas de ejecución

1. Consultas agregadas optimizadas con índices
2. Exportaciones en streaming para datasets grandes
3. Restricciones estrictas por rol (`admin`) en todas las rutas
4. Paginación cursor-based para listas grandes

### Criterios de salida

- Dashboard con tiempos de respuesta estables (<200ms)
- Exportaciones funcionales sin consumo excesivo de memoria

---

## Agente: Testing and QA

### Objetivo

Asegurar calidad con pipeline reproducible en local y CI.

### Stack de testing

- `pytest-asyncio` con `asyncio_mode = "auto"` (no decorar tests con `@pytest.mark.asyncio`)
- `httpx.AsyncClient` con ASGI transport (sin servidor HTTP real)
- `aiosqlite` como DB in-memory para tests (no PostgreSQL)
- `moto[s3]` para mock de operaciones S3-compatible
- `factory-boy` para generación de datos de test
- Rate limiter deshabilitado globalmente en tests

### Reglas de ejecución

1. Unit tests para reglas de negocio (services)
2. Integration tests para endpoints críticos (API)
3. Mock de storage con `moto[s3]` — no levantar MinIO
4. Ejecutar siempre antes de commit: `make lint && make test`
5. Cobertura mínima: 70% (`make test-cov`)

### Criterios de salida

- Cambios sin regresión detectable
- Cobertura ≥70% en rutas y reglas críticas
- Zero warnings de ruff y mypy

---

## Agente: AI Agent / LangGraph

### Objetivo

Implementar y mantener el workflow de agente LangGraph para procesamiento de documentos y chat.

### Arquitectura del workflow

```
Input → [router] → chat mode     → [chat node] → END
                 → pipeline mode  → [doc_ingestion] → [doc_understanding] → [classification] → [justification] → END
```

### Reglas de ejecución

1. Estado tipado via `AgentState` (TypedDict con `total=False`)
2. Nodos retornan state parcial: `{**state, "key": value}` (spread pattern)
3. Prompts versionados en `app/agent/prompts/` — nunca inline
4. LLM vía `LLMPort` protocol — nunca importar litellm/openai directamente
5. Tools en `app/agent/tools/` — funciones puras que operan sobre datos
6. Registrar token usage para sistema de créditos

### Criterios de salida

- Workflow determinístico y reproducible
- Tests para cada nodo individualmente
- Manejo de errores LLM (fallback chain, reintentos)

---

## Plantilla Operativa (todas las tareas)

```
1. Entender caso de uso y constraints (rol, estado, reglas)
2. Definir/ajustar schema Pydantic de entrada/salida
3. Implementar service con validaciones de negocio
4. Exponer endpoint con dependencias de seguridad
5. Agregar tests unitarios + integración
6. Ejecutar: make lint && make test
7. Verificar impacto en migraciones/documentación
```

### Regla de oro

> El core de negocio (`services/`, `agent/`, `models/`) **nunca** importa SDKs de cloud.
> Solo interactúa con `Port` protocols definidos en `app/adapters/`.
