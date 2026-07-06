# Testing Agéntico — Referencia Completa

## Estructura de Tests

```
tests/
├── conftest.py                       # Fixtures globales (db, user, mocks)
├── agent/
│   ├── test_router_node.py           # Tests de routing/clasificación
│   ├── test_chat_node.py
│   ├── test_pipeline_node.py
│   ├── test_email_fetch_node.py
│   ├── test_extraction_node.py
│   └── test_generate_activities_node.py
├── test_agent_pipeline.py            # Tests de integración del grafo completo
├── test_auth_api.py
├── test_contrato_api.py
└── test_cuenta_cobro_api.py
```

## Fixtures Globales (conftest.py)

```python
# tests/conftest.py
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.core.database import get_db
from app.models.usuario import Usuario
from app.agent.state import AgentState


# ─── DB Mock ───────────────────────────────────────────────
@pytest.fixture
def mock_db():
    """AsyncSession mock para tests sin BD real."""
    db = AsyncMock(spec=AsyncSession)
    db.execute.return_value = AsyncMock()
    db.commit.return_value = None
    db.rollback.return_value = None
    return db


# ─── LLM Mock ──────────────────────────────────────────────
@pytest.fixture
def mock_llm():
    """Mock LiteLLM — tests sin costo ni latencia de red."""
    with patch("app.adapters.llm.litellm_adapter.acompletion") as m:
        m.return_value = fake_llm_response(
            content='{"resultado": "respuesta del LLM mockeada"}'
        )
        yield m


@pytest.fixture
def mock_llm_text():
    """LLM que retorna texto libre (no JSON)."""
    with patch("app.adapters.llm.litellm_adapter.acompletion") as m:
        m.return_value = fake_llm_response(content="Respuesta narrativa del LLM.")
        yield m


def fake_llm_response(content: str):
    """Construye una respuesta LiteLLM fake."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage.total_tokens = 150
    response.model = "gemini/gemini-2.5-flash"
    return response


# ─── Gmail Mock ────────────────────────────────────────────
@pytest.fixture
def mock_gmail():
    """GmailAdapter mock — tests sin OAuth ni Gmail real."""
    with patch("app.adapters.email.gmail_adapter.GmailAdapter") as m:
        instance = m.return_value
        instance.search_messages.return_value = []
        instance.get_message_content.return_value = {"subject": "Test", "body": ""}
        yield instance


# ─── Drive Mock ────────────────────────────────────────────
@pytest.fixture
def mock_drive():
    """DriveAdapter mock."""
    with patch("app.adapters.drive.drive_adapter.DriveAdapter") as m:
        instance = m.return_value
        instance.upload_file.return_value = {"id": "fake-drive-id", "webViewLink": "https://drive.google.com/fake"}
        instance.create_folder.return_value = {"id": "fake-folder-id"}
        yield instance


# ─── Storage Mock ──────────────────────────────────────────
@pytest.fixture
def mock_storage():
    """S3Adapter mock — tests sin MinIO/R2."""
    with patch("app.adapters.storage.s3_adapter.S3Adapter") as m:
        instance = m.return_value
        instance.upload.return_value = "https://storage.example.com/fake.pdf"
        yield instance


# ─── Usuario de prueba ─────────────────────────────────────
@pytest_asyncio.fixture
async def test_user(mock_db) -> Usuario:
    from uuid import uuid4
    user = Usuario(
        id=uuid4(),
        email="test@cashin.app",
        nombre="Usuario Test",
        hashed_password="$2b$12$fake",
    )
    return user


# ─── HTTP Client ───────────────────────────────────────────
@pytest_asyncio.fixture
async def client(mock_db, test_user):
    """AsyncClient con BD y usuario autenticados."""
    app.dependency_overrides[get_db] = lambda: mock_db
    async with AsyncClient(app=app, base_url="http://test") as c:
        # Inyectar token JWT válido
        token = create_test_token(test_user.id)
        c.headers["Authorization"] = f"Bearer {token}"
        yield c
    app.dependency_overrides.clear()
```

## Plantilla de Test de Nodo (Unitario)

```python
# tests/agent/test_mi_nodo.py
import pytest
import pytest_asyncio
from uuid import uuid4
from unittest.mock import AsyncMock, patch

from app.agent.state import AgentState
from app.agent.nodes.mi_modo import mi_nodo


@pytest.mark.asyncio
async def test_mi_nodo_caso_exitoso(mock_llm, mock_db):
    """Verifica que el nodo retorna el campo esperado."""
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        mode="MI_MODO",
        user_input="input de prueba",
        contrato_contexto={"entidad": "ICBF", "valor": 5000000},
        _db=mock_db,
    )
    mock_llm.return_value = fake_llm_response(
        content='{"resultado": "Actividad completada según contrato"}'
    )

    result = await mi_nodo(state)

    assert "mi_campo_resultado" in result
    assert result["mi_campo_resultado"] is not None
    assert mock_llm.called


@pytest.mark.asyncio
async def test_mi_nodo_maneja_error_llm(mock_db):
    """Verifica que el nodo maneja errores del LLM correctamente."""
    state = AgentState(
        session_id=uuid4(),
        user_id=uuid4(),
        _db=mock_db,
    )

    with patch("app.adapters.llm.litellm_adapter.acompletion") as m:
        m.side_effect = Exception("LLM timeout")

        from app.core.exceptions import AgentError
        with pytest.raises(AgentError):
            await mi_nodo(state)


@pytest.mark.asyncio
async def test_mi_nodo_no_modifica_campos_ajenos(mock_llm, mock_db):
    """Verifica que el nodo solo retorna los campos que le corresponden."""
    state = AgentState(session_id=uuid4(), user_id=uuid4(), _db=mock_db)

    result = await mi_nodo(state)

    # Solo debe contener los campos que este nodo produce
    allowed_fields = {"mi_campo_resultado", "awaiting_human_approval"}
    assert set(result.keys()).issubset(allowed_fields)
```

## Plantilla de Test de Integración (Grafo Completo)

```python
# tests/test_agent_pipeline.py
@pytest.mark.asyncio
async def test_pipeline_mode_extrae_obligaciones(
    client, mock_llm, mock_storage, test_user, mock_db
):
    """Test end-to-end del modo PIPELINE."""
    # 1. Crear sesión del agente
    resp = await client.post("/api/v1/agent/sessions")
    assert resp.status_code == 201
    session_id = resp.json()["session_id"]

    # 2. Subir un contrato PDF
    pdf_content = b"%PDF-1.4 fake contract content"
    resp = await client.post(
        f"/api/v1/agent/sessions/{session_id}/upload",
        files={"file": ("contrato.pdf", pdf_content, "application/pdf")},
    )
    assert resp.status_code == 200

    # 3. Ejecutar el agente en modo PIPELINE
    mock_llm.return_value = fake_llm_response(
        content='{"obligaciones": [{"descripcion": "Entregar informe mensual"}]}'
    )
    resp = await client.post(
        f"/api/v1/agent/sessions/{session_id}/run",
        json={"mode": "EXTRACT_OBLIGATIONS"},
    )
    assert resp.status_code == 200

    # 4. Verificar resultado
    state_resp = await client.get(f"/api/v1/agent/sessions/{session_id}/state")
    assert state_resp.json()["obligaciones_extraidas"] is not None
    assert len(state_resp.json()["obligaciones_extraidas"]) > 0
```

## Mocks Específicos por Escenario

```python
# Mock de email con evidencia relevante
FAKE_EMAIL_WITH_EVIDENCE = {
    "id": "msg_123",
    "subject": "Informe de actividades enero 2026",
    "from": "contratista@example.com",
    "to": "supervisor@icbf.gov.co",
    "date": "2026-01-31",
    "body": "Adjunto el informe mensual de actividades correspondiente a enero...",
    "snippet": "Informe mensual de actividades",
}

@pytest.fixture
def mock_gmail_with_evidence(mock_gmail):
    mock_gmail.search_messages.return_value = [FAKE_EMAIL_WITH_EVIDENCE]
    return mock_gmail


# Mock de LLM para matching de evidencia
@pytest.fixture
def mock_llm_matching():
    with patch("app.adapters.llm.litellm_adapter.acompletion") as m:
        m.return_value = fake_llm_response(content=json.dumps({
            "matches": [{
                "obligacion_id": "oblig-1",
                "email_id": "msg_123",
                "confidence": 0.92,
                "reasoning": "El email menciona explícitamente el informe mensual de la obligación 1."
            }]
        }))
        yield m
```

## Cobertura — Objetivo 70%

```bash
# Ejecutar tests con reporte de cobertura
make test

# Ver reporte detallado por módulo
uv run pytest --cov=app --cov-report=term-missing --cov-fail-under=70

# Módulos con cobertura mínima obligatoria:
# app/agent/nodes/    → 80%
# app/services/       → 75%
# app/api/v1/         → 70%
# app/adapters/       → 60% (mocks cubren el resto)
```

## Reglas de Testing Agéntico

1. **Nunca llamar LLM real en tests** — siempre mockear `acompletion`
2. **Nunca usar BD real en tests unitarios** — AsyncMock para `AsyncSession`
3. **Tests de integración usan BD en memoria** (SQLite async o PostgreSQL de test)
4. **Cada nodo tiene su propio archivo de test**
5. **Nombres descriptivos**: `test_{nodo}_{escenario}_{resultado_esperado}`
6. **Los fixtures de conftest.py son la única fuente de mocks compartidos**
