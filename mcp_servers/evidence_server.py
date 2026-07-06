"""Evidence MCP Server — expone el agente 'explorer' de evidencias a clientes MCP.

Tool:
- descubrir_evidencias: explora Gmail + Drive + Calendar para justificar obligaciones
  contractuales y devuelve, por obligación, la justificación y los links de soporte.

Proxea al backend CashIn (POST /integraciones/evidencias/descubrir) y requiere un Bearer
token válido (CASHIN_BEARER_TOKEN) del usuario cuya cuenta de Google está conectada.
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = os.getenv("CASHIN_API_URL", "http://localhost:8000/api/v1")
BEARER_TOKEN = os.getenv("CASHIN_BEARER_TOKEN", "")

mcp = FastMCP("cashin-evidence")

_HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}", "Content-Type": "application/json"}


@mcp.tool()
async def descubrir_evidencias(
    obligaciones: list[str],
    fecha_inicio: str,
    fecha_fin: str,
    supervisor_email: str = "",
    entidad: str = "",
) -> dict:  # type: ignore[type-arg]
    """Explora Gmail, Drive y Calendar para justificar el cumplimiento de obligaciones.

    Args:
        obligaciones: Lista de descripciones de obligaciones contractuales a justificar.
        fecha_inicio: Inicio del período en formato YYYY-MM-DD.
        fecha_fin: Fin del período en formato YYYY-MM-DD.
        supervisor_email: (Opcional) correo del supervisor para mejorar la búsqueda.
        entidad: (Opcional) nombre de la entidad contratante.

    Returns:
        Dict con, por obligación, el texto de justificación y los links a las evidencias
        encontradas (correos, documentos de Drive, eventos de Calendar).
    """
    payload: dict = {
        "obligaciones": [{"descripcion": d} for d in obligaciones],
        "fecha_inicio": fecha_inicio,
        "fecha_fin": fecha_fin,
    }
    if supervisor_email:
        payload["supervisor_email"] = supervisor_email
    if entidad:
        payload["entidad"] = entidad

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{BASE_URL}/integraciones/evidencias/descubrir",
            headers=_HEADERS,
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()


if __name__ == "__main__":
    mcp.run()
