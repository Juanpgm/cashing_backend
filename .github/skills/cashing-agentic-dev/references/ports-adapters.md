# Ports & Adapters — Referencia Completa

## Checklist de 8 Pasos para Nueva Integración

```
1. app/adapters/{servicio}/port.py           → Protocol con type hints completos
2. app/adapters/{servicio}/{impl}_adapter.py → Implementación concreta
3. app/services/{servicio}_service.py        → Lógica de negocio via Port
4. app/api/v1/{servicio}.py                  → Endpoints FastAPI
5. mcp_servers/{servicio}_server.py          → MCP server standalone (proxy)
6. .claude/settings.json                     → Registrar el MCP server
7. alembic/versions/XXX_{servicio}.py        → Migración si hay tablas nuevas
8. tests/test_{servicio}_*.py                → Tests unitarios + integración
```

## Estructura de un Port (Protocol)

```python
# app/adapters/{servicio}/port.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class MiServicioPort(Protocol):
    """Contrato del servicio. Nunca importar la implementación concreta."""

    async def buscar(self, query: str, user_id: str) -> list[dict]:
        """Descripción del método con tipos explícitos."""
        ...

    async def obtener(self, item_id: str) -> dict | None:
        ...
```

## Estructura del Adapter Concreto

```python
# app/adapters/{servicio}/{impl}_adapter.py
from app.adapters.{servicio}.port import MiServicioPort

class MiServicioAdapter:
    """Implementación concreta que satisface MiServicioPort."""

    def __init__(self, credentials: dict):
        self._client = build_client(credentials)

    async def buscar(self, query: str, user_id: str) -> list[dict]:
        # Implementación real
        ...
```

## Regla de Aislamiento (Absoluta)

```
app/services/    → usa Protocol (Port)        ✅
app/agent/nodes/ → usa Protocol (Port)        ✅
app/adapters/    → implementa Protocol        ✅
app/api/         → inyecta adaptador concreto ✅

app/services/    → import boto3               ❌
app/agent/nodes/ → import googleapiclient     ❌
app/adapters/    → importa otro adapter       ❌
```

## Google APIs — Reglas Especiales

Las Google APIs son síncronas en el SDK Python. **SIEMPRE** envolver con `run_in_executor`:

```python
import asyncio
from functools import partial

async def buscar_emails(self, query: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    
    # ✅ run_in_executor para no bloquear el event loop
    result = await loop.run_in_executor(
        None,
        partial(
            self._service.users().messages().list,
            userId="me",
            q=query,
            maxResults=50
        )
    )
    response = await loop.run_in_executor(None, result.execute)
    return response.get("messages", [])
    
    # ❌ Llama síncrona — bloquea el event loop de FastAPI
    # result = self._service.users().messages().list(userId="me", q=query).execute()
```

## OAuth Tokens — Cifrado Obligatorio

Los tokens OAuth de Google NUNCA se guardan en texto plano:

```python
from cryptography.fernet import Fernet
from app.core.config import settings

cipher = Fernet(settings.FERNET_KEY.encode())

# Cifrar antes de guardar en BD
def encrypt_token(token_dict: dict) -> str:
    token_json = json.dumps(token_dict)
    return cipher.encrypt(token_json.encode()).decode()

# Descifrar al recuperar
def decrypt_token(encrypted: str) -> dict:
    decrypted = cipher.decrypt(encrypted.encode()).decode()
    return json.loads(decrypted)
```

## MCP Server — Patrón Proxy

Los MCP servers son procesos independientes que NUNCA llaman APIs externas directamente:

```python
# mcp_servers/{servicio}_server.py
import httpx
from mcp.server import Server
from mcp.server.models import InitializationOptions

server = Server("{servicio}-server")

@server.tool("{nombre_herramienta}")
async def mi_herramienta(arg1: str, user_id: str) -> dict:
    """Descripción de la herramienta para el LLM."""
    # ✅ Llama a la FastAPI de CashIn (proxy)
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{CASHIN_API_URL}/api/v1/{servicio}/accion",
            params={"arg1": arg1, "user_id": user_id},
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            timeout=30.0
        )
    return response.json()
    
    # ❌ NO llamar directamente a la API externa
    # from googleapiclient.discovery import build  # NUNCA en mcp_servers/
```

## Registrar MCP Server en .claude/settings.json

```json
{
  "mcpServers": {
    "{servicio}": {
      "command": "python",
      "args": ["mcp_servers/{servicio}_server.py"],
      "env": {
        "CASHIN_API_URL": "http://localhost:8000",
        "SERVICE_TOKEN": "${CASHIN_SERVICE_TOKEN}"
      }
    }
  }
}
```

## Adaptadores Existentes (Referencia)

| Puerto | Adaptador Dev | Adaptador Prod |
|--------|--------------|----------------|
| `LLMPort` | `OllamaAdapter` (qwen2.5:7b) | `LiteLLMAdapter` (Gemini→Groq) |
| `StoragePort` | `S3Adapter` (MinIO local) | `S3Adapter` (Cloudflare R2) |
| `EmailPort` | Mock/stub en tests | `GmailAdapter` (OAuth 2.0) |
| `DrivePort` | Mock/stub en tests | `DriveAdapter` (OAuth 2.0) |
