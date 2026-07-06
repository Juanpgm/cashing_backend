"""SECOP client — thin async facade used by secop_discovery_node.

Wraps the existing secop_service functions so that the agent node
does not depend directly on the service layer (ports & adapters).
"""
from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import secop_service

logger = structlog.get_logger("agent.tools.secop_client")


async def discover_contracts(
    db: AsyncSession,
    cedula: str,
    *,
    refresh: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Fetch SECOP contracts and their documents for a given cédula.

    Returns:
        contratos: list of SecopContratoResponse dicts (serialised)
        documentos: flat list of SecopDocumentoResponse dicts for all contracts
    """
    try:
        contrato_objs = await secop_service.buscar_contratos_cedula(
            db, cedula, refresh=refresh
        )
    except Exception as exc:
        await logger.aerror("secop_client.contracts_failed", cedula=cedula, error=str(exc))
        return [], []

    contratos = [c.model_dump() for c in contrato_objs]

    documentos: list[dict] = []
    for contrato in contrato_objs:
        numero = getattr(contrato, "numero_contrato", None) or contrato.model_dump().get(
            "numero_contrato"
        )
        if not numero:
            continue
        try:
            doc_objs = await secop_service.buscar_documentos_contrato(
                db, numero, refresh=refresh
            )
            documentos.extend(d.model_dump() for d in doc_objs)
        except Exception as exc:
            await logger.awarning(
                "secop_client.docs_failed", numero_contrato=numero, error=str(exc)
            )

    return contratos, documentos
