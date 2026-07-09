"""Live-LLM test for the agentic tool-calling loop — real Ollama drives multi-hop
tool calls end-to-end through `agent_chat_service.chat_with_tools`.

No IDs are given in the user message: the flagship test (Test A) requires the
model to discover the contrato_id on its own (via `listar_contratos`) before it
can call `crear_cuenta_cobro`. This is the whole point of the discovery tools
added in app/tools/catalog/listar_contratos.py and listar_cuentas_cobro.py — a
small local model needs a tool that hands it the ID rather than being asked to
invent or guess one.

Runtime expectations: llama3.1:8b chaining 2+ tool calls can take 30-90s per
test (see tests/live/conftest.py for the shared Ollama-availability/settings
fixtures).
"""

from __future__ import annotations

from datetime import date

import app.tools.catalog  # noqa: F401 — registers every catalog tool (listar_* included)
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.usuario import Usuario
from app.services import agent_chat_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.live_llm


async def _make_user_with_contrato(db: AsyncSession) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email="tool_loop_live@example.com",
        nombre="Tool Loop Live User",
        cedula="50505050",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="TLL-0001",
        objeto="Prestación de servicios profesionales de apoyo técnico y administrativo",
        entidad="Alcaldía de Prueba",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor="50505050",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.asyncio
async def test_agent_creates_cuenta_cobro_by_discovering_contrato(db: AsyncSession) -> None:
    """2-hop agentic flow: the model must find the contrato_id itself, then create
    the cuenta de cobro — no ID is given in the user message.

    Generous latitude for a small local model: extra harmless tool calls (e.g. a
    second `listar_contratos`, or `listar_cuentas_cobro` to check for duplicates)
    are fine as long as the cuenta de cobro ends up created for the right
    contrato/mes/anio.
    """
    user, contrato = await _make_user_with_contrato(db)

    result = await agent_chat_service.chat_with_tools(
        db, user, "Créame la cuenta de cobro de julio de 2026 para mi contrato", None, {}
    )

    assert result.content.strip() != ""

    tool_names = [ev.tool for ev in result.tool_events]
    assert "listar_contratos" in tool_names, f"model never discovered the contrato_id; tool_events={tool_names}"
    assert "crear_cuenta_cobro" in tool_names, f"model never created the cuenta; tool_events={tool_names}"

    crear_events = [ev for ev in result.tool_events if ev.tool == "crear_cuenta_cobro"]
    assert any(ev.status == "ok" for ev in crear_events), f"crear_cuenta_cobro never succeeded: {crear_events}"

    rows = await db.execute(
        select(CuentaCobro).where(
            CuentaCobro.contrato_id == contrato.id,
            CuentaCobro.mes == 7,
            CuentaCobro.anio == 2026,
        )
    )
    cuenta = rows.scalar_one_or_none()
    assert cuenta is not None, "no CuentaCobro row exists for contrato/julio/2026 after the agent loop"


@pytest.mark.asyncio
async def test_agent_conversational_message_does_not_write(db: AsyncSession) -> None:
    """A purely conversational message must not create/mutate any data — tool_events
    may be empty, or contain only read-only calls."""
    user, _contrato = await _make_user_with_contrato(db)

    result = await agent_chat_service.chat_with_tools(db, user, "¿Qué puedes hacer por mí?", None, {})

    assert result.content.strip() != ""

    rows = await db.execute(select(CuentaCobro).where(CuentaCobro.contrato_id == _contrato.id))
    assert rows.scalars().all() == []

    from app.tools.registry import TOOL_REGISTRY

    for ev in result.tool_events:
        if ev.status != "ok":
            continue
        spec = TOOL_REGISTRY.get(ev.tool)
        if spec is not None:
            assert "write" not in spec.tags, f"conversational message triggered a write tool: {ev.tool}"
