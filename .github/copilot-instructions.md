# Copilot Instructions - CashIn Backend

Estas instrucciones hacen que Copilot use `.github/INSTRUCTIONS.md`, `.github/SKILLS.md`, `.github/TOOLS.md` y `.github/AGENTS.md` como base canónica para sugerencias de arquitectura, implementación y operación.

## Fuente de verdad del proyecto

1. `.github/INSTRUCTIONS.md` — Arquitectura, reglas de código, roadmap y flujo operativo.
2. `.github/SKILLS.md` — Competencias técnicas y criterios de diseño cloud-agnostic.
3. `.github/TOOLS.md` — Stack de desarrollo local, QA, emulación y deploy Railway.
4. `.github/AGENTS.md` — Playbooks tácticos por tipo de tarea (API, dominio, datos, auth, pagos, QA).

Si hay conflicto entre sugerencias genéricas y estos documentos, **priorizar estos documentos**.

## Filosofía cloud-agnostic

- **MVP en Railway** — sin dependencia de AWS, GCP ni Azure.
- Arquitectura **Ports & Adapters**: toda interacción con servicios externos via `Protocol` en `app/adapters/`.
- El core de negocio (`services/`, `agent/`, `models/`) **nunca** importa SDKs de cloud.
- Storage: `StoragePort` → S3-compatible (MinIO dev, Cloudflare R2 prod).
- LLM: `LLMPort` → LiteLLM (Gemini, OpenAI, Ollama).
- Auth: JWT custom (python-jose HS256) — sin Cognito/Firebase.
- Migración futura a cloud: solo escribir nuevo adapter, no refactorizar core.

## Reglas para sugerencias de código

- Mantener arquitectura por capas: `api → services → models/persistence`.
- Python 3.12, asincronía nativa y tipado estricto (`mypy --strict`).
- `AsyncSession` SQLAlchemy 2.0, Pydantic v2 y excepciones centralizadas.
- No acoplar al proveedor cloud; abstraer todo via Ports & Adapters.
- `Decimal` para montos, `structlog` para logging, `UTC` siempre.

## Reglas para sugerencias de pruebas

- Pruebas unitarias para reglas de negocio (services).
- Pruebas de integración para endpoints críticos (API).
- `pytest-asyncio` (auto mode), `httpx` (AsyncClient), `moto[s3]` (storage mock).
- SQLite in-memory (`aiosqlite`) como DB de test.
- Cobertura mínima: 70%.

## Reglas para sugerencias de tooling

- `uv` como gestor de paquetes.
- Validar siempre: `make lint && make test` antes de cerrar tareas.
- Deploy: Railway (Dockerfile + railway.toml). Sin Lambda, Cloud Functions, ni Container Apps.

## Estilo de respuesta esperado

- Sugerencias accionables y concretas.
- Evitar placeholders ambiguos.
- Respetar nomenclatura y estructura del proyecto.
- Código debe funcionar sin servicios cloud — solo Railway + PostgreSQL + R2/MinIO.
