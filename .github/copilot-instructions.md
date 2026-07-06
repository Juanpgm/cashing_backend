# Copilot Instructions - CashIn Backend

Estas instrucciones hacen que Copilot use `.github/INSTRUCTIONS.md`, `.github/SKILLS.md`, `.github/TOOLS.md` y `.github/AGENTS.md` como base canónica para sugerencias de arquitectura, implementación y operación.

## Fuente de verdad del proyecto

1. `.github/INSTRUCTIONS.md` — Arquitectura, reglas de código, roadmap y flujo operativo.
2. `.github/SKILLS.md` — Competencias técnicas y criterios de diseño cloud-agnostic.
3. `.github/TOOLS.md` — Stack de desarrollo local, QA, emulación y deploy Railway.
4. `.github/AGENTS.md` — Playbooks tácticos por tipo de tarea (API, dominio, datos, auth, pagos, QA, evidencia, MCP).
5. `.github/instructions/cashing-agentic-saas.instructions.md` — **Paradigma rector agéntico SaaS**: filosofía agent-first, LangGraph patterns, HIL, multi-tenancy, créditos, observabilidad, roadmap de fases.
6. `.github/skills/cashing-agentic-dev/SKILL.md` — **Skill de desarrollo agéntico**: procedimientos paso a paso para agregar nodos LangGraph, integraciones Ports & Adapters, HIL, generación de documentos, tests agénticos, y patrones SaaS. Cargar cuando se implementa cualquier feature del agente.

Si hay conflicto entre sugerencias genéricas y estos documentos, **priorizar estos documentos**.

## Filosofía cloud-agnostic + agentic-first

- **MVP en Railway** — sin dependencia de AWS, GCP ni Azure.
- Arquitectura **Ports & Adapters**: toda interacción con servicios externos via `Protocol` en `app/adapters/`.
- El core de negocio (`services/`, `agent/`, `models/`) **nunca** importa SDKs de cloud ni Google APIs directamente.
- Storage: `StoragePort` → S3-compatible (MinIO dev, Cloudflare R2 prod).
- LLM: `LLMPort` → LiteLLM (Gemini → Groq → Ollama fallback chain).
- Email: `EmailPort` → `GmailAdapter` (tokens OAuth cifrados con Fernet).
- Drive: `DrivePort` → `DriveAdapter` (tokens compartidos con Gmail).
- Calendar: `CalendarPort` → `GoogleCalendarAdapter`.
- MCP: servidores standalone en `mcp_servers/` que proxean al backend FastAPI.
- Auth: JWT custom (python-jose HS256) — sin Cognito/Firebase.
- Migración futura a cloud: solo escribir nuevo adapter, no refactorizar core.

## Reglas para sugerencias de código

- Mantener arquitectura por capas: `api → services → agent/nodes → adapters`.
- Python 3.12, asincronía nativa y tipado estricto (`mypy --strict`).
- `AsyncSession` SQLAlchemy 2.0, Pydantic v2 y excepciones centralizadas.
- No acoplar al proveedor cloud ni Google APIs; abstraer todo via Ports & Adapters.
- Google API calls siempre en `run_in_executor` (son síncronas).
- Tokens OAuth: encriptar con Fernet antes de guardar, desencriptar en memoria.
- `Decimal` para montos, `structlog` para logging, `UTC` siempre.
- MCP servers: no conectar a Google directamente — llamar a la API FastAPI.

## Reglas para nuevas integraciones (Google Workspace / otros)

1. Crear `adapters/{servicio}/port.py` con Protocol
2. Crear `adapters/{servicio}/{proveedor}_adapter.py` con implementación
3. Crear service en `services/{servicio}_service.py`
4. Exponer endpoints OAuth + operaciones en `api/v1/integraciones.py`
5. Crear MCP server en `mcp_servers/{servicio}_server.py`
6. Registrar MCP server en `.claude/settings.json`

## Reglas para sugerencias de pruebas

- Pruebas unitarias para reglas de negocio (services).
- Pruebas de integración para endpoints críticos (API).
- `pytest-asyncio` (auto mode), `httpx` (AsyncClient), `moto[s3]` (storage mock).
- SQLite in-memory (`aiosqlite`) como DB de test.
- Google APIs: mock con `unittest.mock.patch` o credenciales sandbox.
- Cobertura mínima: 70%.

## Reglas para sugerencias de tooling

- `uv` como gestor de paquetes.
- Validar siempre: `make lint && make test` antes de cerrar tareas.
- Deploy: Railway (Dockerfile + railway.toml). Sin Lambda, Cloud Functions, ni Container Apps.
- MCP servers: ejecutar con `uv run python mcp_servers/{server}.py`.

## Estilo de respuesta esperado

- Sugerencias accionables y concretas.
- Evitar placeholders ambiguos.
- Respetar nomenclatura y estructura del proyecto.
- Código debe funcionar sin servicios cloud — solo Railway + PostgreSQL + R2/MinIO + Google OAuth.
