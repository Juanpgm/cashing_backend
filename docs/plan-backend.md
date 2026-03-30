# Plan: CashIn AI Agent Backend

## TL;DR

Reemplazar el plan-backend.md existente (CRUD-first) por un sistema **AI Agent-first** que automatiza la creación de cuentas de cobro para contratistas colombianos. El agente procesa contratos, extrae actividades, recolecta evidencia de Google Workspace, filtra contenido no-laboral, genera justificaciones y ensambla los documentos finales (PDF) con carpetas de evidencias organizadas.

**Stack principal**: FastAPI 3.12 + LangGraph + LiteLLM + PostgreSQL + Cloudflare R2 (storage)
**Deploy MVP**: Railway (backend + DB) — con configs para Docker, GCP Cloud Run y AWS Lambda
**Monetización**: Sistema de créditos (pago por cuenta vía Wompi + suscripción mensual + tokens IA)

---

## 0. Recomendaciones de Stack

### Backend Framework

| Opción         | Veredicto       | Razón                                                                              |
| -------------- | --------------- | ---------------------------------------------------------------------------------- |
| **FastAPI**    | **RECOMENDADO** | Async nativo, tipado estricto, OpenAPI auto, mejor ecosistema para AI/ML en Python |
| Django         | Descartado      | Más pesado, ORM síncrono por defecto, overhead para agentes IA                     |
| Express/NestJS | Descartado      | Python tiene mejor ecosistema de IA/ML                                             |

### Motor de Agente IA

| Opción          | Veredicto       | Razón                                                                                             |
| --------------- | --------------- | ------------------------------------------------------------------------------------------------- |
| **LangGraph**   | **RECOMENDADO** | Grafos de workflow, state management, streaming SSE, tool calling, persistence, human-in-the-loop |
| CrewAI          | Alternativa     | Buenos multi-agentes pero menos control de flujo                                                  |
| Google ADK      | Descartado MVP  | Muy nuevo (2025), acoplado a Google                                                               |
| Custom (propio) | Descartado MVP  | Mucho código para reinventar state management y streaming                                         |

### Abstracción LLM

| Opción      | Veredicto       | Razón                                                                                   |
| ----------- | --------------- | --------------------------------------------------------------------------------------- |
| **LiteLLM** | **RECOMENDADO** | 100+ modelos, tracking de costos, fallback chains, cambio de proveedor sin tocar código |

### Modelos LLM (por tier de costo)

| Tier           | Modelo                         | Costo aprox.          | Uso                                     |
| -------------- | ------------------------------ | --------------------- | --------------------------------------- |
| **Económico**  | Gemini 2.0 Flash-Lite          | ~$0.075/1M tokens     | Clasificación, filtrado, tareas simples |
| **Balanceado** | Gemini 2.0 Flash / GPT-4o-mini | ~$0.15-0.30/1M tokens | Justificaciones, extracción             |
| **Calidad**    | Claude Sonnet 4 / GPT-4o       | ~$3-5/1M tokens       | Análisis complejo de contratos          |
| **Local**      | Ollama + Llama 3.1/Qwen 2.5    | $0 (solo hardware)    | Desarrollo, testing, privacidad         |

### Base de Datos

| Opción            | Veredicto       | Razón                                               |
| ----------------- | --------------- | --------------------------------------------------- |
| **PostgreSQL 16** | **RECOMENDADO** | Relacional + pgvector para embeddings de documentos |
| MongoDB           | Descartado      | Los datos contractuales son altamente relacionales  |
| SQLite            | Solo testing    | Sin concurrencia para producción                    |

### Object Storage (Evidencias + PDFs)

| Opción            | Costo            | Razón                                     |
| ----------------- | ---------------- | ----------------------------------------- |
| **Cloudflare R2** | Free 10GB/mo     | S3-compatible, sin egress fees, ideal MVP |
| AWS S3            | ~$0.023/GB       | Estándar pero egress caro                 |
| GCS               | ~$0.020/GB       | Alternativa GCP                           |
| MinIO             | $0 (self-hosted) | Desarrollo local                          |

### Deploy

| Plataforma          | Costo MVP | Complejidad | Mejor Para               |
| ------------------- | --------- | ----------- | ------------------------ |
| **Railway**         | $5-10/mes | Muy baja    | **MVP** (recomendado)    |
| GCP Cloud Run       | $0-15/mes | Baja        | Escala automática        |
| AWS Lambda + API GW | $0-5/mes  | Media       | Alta escala, pay-per-use |
| Docker (VPS)        | $5-20/mes | Media       | Control total            |

### Auth

| Opción                                | Veredicto           | Razón                                          |
| ------------------------------------- | ------------------- | ---------------------------------------------- |
| **JWT Custom (bcrypt + python-jose)** | **RECOMENDADO MVP** | Sin vendor lock-in, cero costo, simple         |
| AWS Cognito                           | Post-MVP            | Acopla a AWS, complejidad innecesaria para MVP |
| Firebase Auth                         | Alternativa         | Buen free tier, pero acopla a Google           |
| Supabase Auth                         | Alternativa         | Free tier generoso, PostgreSQL nativo          |

---

## 1. Arquitectura del Agente

### Grafo Principal (LangGraph)

```
[User Input] → [Router Node]
                    ↓
    ┌───────────────┼───────────────┐
    ↓               ↓               ↓
[Chat Mode]  [Pipeline Mode]  [Config Mode]
    ↓               ↓               ↓
    ↓        [Doc Ingestion]  [Template Mgmt]
    ↓               ↓
    ↓        [Doc Understanding]
    ↓               ↓
    ↓        [Evidence Collection]
    ↓               ↓
    ↓        [Evidence Classification]
    ↓               ↓
    ↓        [Justification Generation]
    ↓               ↓
    ↓        [Document Assembly]
    ↓               ↓
    └───────→ [Output / Response]
```

### Tools del Agente

- `parse_docx`: Extraer texto y estructura de .docx
- `parse_pdf`: Extraer texto de PDF
- `parse_excel`: Leer datos de Excel
- `search_gmail`: Buscar correos en rango de fechas
- `search_calendar`: Buscar eventos en rango
- `search_drive`: Buscar archivos en rango
- `classify_content`: Clasificar como laboral/no-laboral
- `generate_justification`: Generar texto de justificación
- `fill_template`: Llenar plantilla con datos
- `generate_pdf`: Crear PDF final
- `organize_evidence`: Crear estructura de carpetas
- `upload_file`: Subir archivo a storage

---

## 2. Estructura del Proyecto

```
cashin-backend/
├── app/
│   ├── api/
│   │   ├── v1/
│   │   │   ├── auth.py
│   │   │   ├── chat.py
│   │   │   ├── contratos.py
│   │   │   ├── cuentas_cobro.py
│   │   │   ├── actividades.py
│   │   │   ├── documentos.py
│   │   │   ├── evidencias.py
│   │   │   ├── plantillas.py
│   │   │   ├── integraciones.py
│   │   │   ├── pagos.py
│   │   │   └── webhooks.py
│   │   ├── deps.py
│   │   └── router.py
│   ├── agent/
│   │   ├── graph.py
│   │   ├── state.py
│   │   ├── nodes/
│   │   ├── tools/
│   │   └── prompts/
│   ├── models/
│   ├── schemas/
│   ├── services/
│   ├── adapters/
│   │   ├── storage/
│   │   ├── llm/
│   │   ├── google/
│   │   └── payments/
│   ├── core/
│   │   ├── config.py
│   │   ├── security.py
│   │   ├── database.py
│   │   ├── exceptions.py
│   │   ├── audit.py
│   │   ├── rate_limit.py
│   │   ├── security_headers.py
│   │   └── file_validation.py
│   ├── templates/
│   └── main.py
├── alembic/
├── tests/
├── scripts/
├── Dockerfile
├── docker-compose.yml
├── railway.toml
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## 3. Fases de Implementación

### FASE 1: Cimientos (Fundación)

> Prioridad: ALTA | Bloquea todo lo demás

- Inicialización del proyecto (pyproject.toml, requirements, Makefile)
- Core: Config, DB, Exceptions, Security (JWT + bcrypt)
- 16 modelos de datos (usuario, contrato, obligacion, cuenta_cobro, actividad, evidencia, plantilla, documento_fuente, conversacion, credito, pago, suscripcion, google_token, audit_log, token_blacklist)
- Alembic async + migración inicial
- Auth JWT (register, login, refresh, RBAC)
- app/main.py + health + deploy files
- Storage adapter (S3-compatible: R2/MinIO/S3)
- Tests base (conftest, health, auth)

### FASE 2: Motor del Agente IA

> Prioridad: ALTA | Core del producto

- LLM adapter (LiteLLM con tiers + fallback + Ollama)
- Agent tools (document_parser, content_classifier, template_filler, pdf_generator, file_organizer)
- LangGraph state + graph (router, chat, pipeline nodes)
- Prompts versionados (system, extraction, classification, justification)
- API del agente (chat, streaming SSE, document upload/process)
- Tests del agente

### FASE 3: Contratos, Cuentas de Cobro y Plantillas

> Prioridad: ALTA | Funcionalidad core

- CRUD Contratos (manual + desde agente)
- Gestión de Plantillas (.docx → Jinja2)
- Motor de Cuentas de Cobro (máquina de estados)
- Generación de documentos (PDF)

### FASE 4: Integración Google Workspace + Evidencias

> Prioridad: MEDIA | Diferenciador del producto

- Google OAuth (gmail.readonly, calendar.readonly, drive.metadata.readonly)
- Adapters Google (gmail, calendar, drive)
- Clasificación inteligente (laboral vs personal)
- Gestión de evidencias (upload, auto-collect, carpetas)

### FASE 5: Pagos y Monetización

> Prioridad: MEDIA | Monetización

- Sistema de créditos
- Planes de suscripción (free/basico/pro)
- Integración Wompi (compra + suscripción + webhook HMAC)

### FASE 6: Ciberseguridad y Hardening (Enterprise)

> Prioridad: ALTA | Transversal desde Fase 1

- Secrets management (secrets/ folder, generate_secrets.py)
- Auth hardening (bcrypt cost 12, JWT blacklist, brute force protection)
- Protección contra inyecciones (SQLAlchemy ORM, no raw SQL, no shell=True)
- Seguridad de archivos (MIME validation, path traversal prevention, presigned URLs)
- Protección de datos (Fernet encryption, HTTPS, data minimization)
- Seguridad de API (rate limiting, CORS, security headers)
- Seguridad del agente IA (prompt injection defense, data leakage prevention)
- Docker hardening (non-root, slim image)
- Audit logging + structured logging (structlog)
- Dependency security (bandit, pip-audit, safety)
- Security tests completos

### FASE 7: Google Cloud CLI Setup

> Prioridad: ALTA | Necesario para Fase 4

- Crear proyecto GCP + habilitar APIs
- Configurar OAuth consent screen + credenciales
- Scripts automatizados (setup_gcloud.ps1, load_secrets.py)
- Pre-commit hooks de seguridad

### FASE 8: Pulido y Production-Ready

> Prioridad: BAJA | Post-MVP

- Procesamiento de instrucciones de cobro
- Importación masiva (CSV/Excel)
- Notificaciones (email)
- Optimización de costos (caché LLM)

---

## 4. Variables de Entorno (.env.example)

```env
DATABASE_URL=postgresql+asyncpg://cashin:password@localhost:5432/cashin
JWT_SECRET_KEY=your-secret-key-min-32-chars
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=15
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
STORAGE_PROVIDER=minio
STORAGE_ENDPOINT=http://localhost:9000
STORAGE_ACCESS_KEY=minioadmin
STORAGE_SECRET_KEY=minioadmin
STORAGE_BUCKET=cashin-evidencias
LLM_DEFAULT_MODEL=gemini/gemini-2.0-flash-lite
LLM_FALLBACK_MODEL=openai/gpt-4o-mini
LLM_LOCAL_MODEL=ollama/llama3.1
GOOGLE_OAUTH_CLIENT_ID=
GOOGLE_OAUTH_CLIENT_SECRET=
GOOGLE_OAUTH_REDIRECT_URI=http://localhost:8000/api/v1/integraciones/google/callback
WOMPI_PUBLIC_KEY=pub_test_xxx
WOMPI_PRIVATE_KEY=prv_test_xxx
WOMPI_EVENTS_SECRET=test_events_xxx
ENVIRONMENT=development
CORS_ORIGINS=["http://localhost:19006","http://localhost:3000"]
TOKEN_ENCRYPTION_KEY=your-fernet-key-here
CREDITS_PER_CUENTA_COBRO=10
CREDITS_PER_CHAT_MESSAGE=1
CREDITS_PER_EVIDENCE_COLLECTION=5
FREE_CREDITS_ON_SIGNUP=30
```

---

## 5. Decisiones

- **Auth**: JWT custom — sin vendor lock-in, migrable a cualquier identity provider
- **Storage**: Cloudflare R2 — cero egress fees, S3-compatible
- **Agent Engine**: LangGraph — madurez, streaming nativo, state management
- **DB**: Railway PostgreSQL — costo mínimo ($5/mo), migrable
- **Monetización**: Créditos unificados (pay-per-use + suscripción)
- **Scope**: Solo backend, API diseñada para mobile-first (React Native/Expo)
- **Google Workspace**: En el MVP como diferenciador
- **Modelos locales**: Ollama desde el inicio para desarrollo sin costo
- **Seguridad**: OWASP Top 10 + audit logging + rate limiting + dependency scanning
- **Secrets**: Carpeta `secrets/` local + pre-commit hooks — Secret Manager para producción
- **GCP**: CLI-first con scripts automatizados
