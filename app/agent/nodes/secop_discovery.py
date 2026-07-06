"""SECOP discovery node — finds contracts for a cedula via SECOP II API."""

from __future__ import annotations

import structlog

from app.agent.state import AgentState
from app.agent.tools import secop_client

logger = structlog.get_logger("agent.nodes.secop_discovery")


async def secop_discovery_node(state: AgentState) -> AgentState:
    """Discover SECOP contracts for the cedula stored in state.

    Reads:
        state.cedula (str)        — Colombian ID number to search
        state._db (AsyncSession)  — injected at runtime by the API layer

    Writes:
        state.secop_contratos     — list of contract dicts (may be empty)
        state.secop_documentos    — flat list of document dicts
        state.current_phase       — "secop_discovery"
        state.onboarding_mode     — "secop"
        state.response            — human-readable summary for the user
    """
    cedula: str | None = state.get("cedula")
    db = state.get("_db")

    updates: AgentState = {
        **state,
        "current_phase": "secop_discovery",
        "onboarding_mode": "secop",
    }

    if not cedula:
        updates["error"] = "cedula requerida para SECOP discovery"
        updates["response"] = (
            "Para buscar tus contratos en SECOP necesito tu número de cédula. "
            "¿Cuál es tu cédula de ciudadanía?"
        )
        return updates

    if not db:
        updates["error"] = "db session not injected"
        updates["response"] = "Error interno: sesión de base de datos no disponible."
        return updates

    await logger.ainfo("secop_discovery.start", cedula=cedula)

    contratos, documentos = await secop_client.discover_contracts(db, cedula)

    updates["secop_contratos"] = contratos
    updates["secop_documentos"] = documentos

    if not contratos:
        updates["response"] = (
            f"No encontré contratos en SECOP para la cédula **{cedula}**. "
            "Verifica que el número sea correcto o ingresa los datos del contrato manualmente."
        )
        updates["onboarding_mode"] = "manual"
        await logger.awarning("secop_discovery.no_contracts", cedula=cedula)
    else:
        n = len(contratos)
        updates["response"] = (
            f"Encontré **{n}** contrato{'s' if n != 1 else ''} en SECOP para la cédula **{cedula}**. "
            "¿Con cuál deseas trabajar?"
        )
        await logger.ainfo("secop_discovery.found", cedula=cedula, n_contratos=n)

    return updates
