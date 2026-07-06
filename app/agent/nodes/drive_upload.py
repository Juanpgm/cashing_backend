"""Drive upload node — sube PDFs y evidencias a Google Drive organizado por contrato."""

from __future__ import annotations

import structlog

from app.adapters.drive.drive_adapter import DriveAdapter, build_contract_drive_path
from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.drive_upload")


async def drive_upload_node(state: AgentState) -> AgentState:
    """Sube el PDF de la cuenta de cobro a Google Drive, organizado por entidad/contrato/período.

    Requiere en state:
    - user_id: UUID del usuario
    - contrato_contexto: dict con entidad, numero_contrato, mes, anio
    - _pdf_bytes: bytes del PDF a subir (inyectado por el service)
    - _pdf_filename: nombre del archivo (inyectado por el service)

    Produce en state:
    - drive_folder_id: ID de la carpeta del período
    - drive_file_ids: lista con el ID del archivo subido
    - drive_share_links: lista con links compartidos (vacía si no se solicitó)
    - response: confirmación para el usuario
    """
    user_id = state.get("user_id")
    if not user_id:
        return {**state, "error": "user_id requerido para subir a Drive"}

    db = state.get("_db")
    if not db:
        return {
            **state,
            "response": "La subida a Drive requiere contexto de base de datos. "
            "Usa el endpoint POST /api/v1/integraciones/drive/upload.",
        }

    pdf_bytes: bytes | None = state.get("_pdf_bytes")
    pdf_filename: str = state.get("_pdf_filename") or "cuenta_cobro.pdf"

    if not pdf_bytes:
        return {
            **state,
            "error": "No hay PDF para subir a Drive",
            "response": "No encontré el PDF de la cuenta de cobro para subir.",
        }

    contrato = state.get("contrato_contexto") or {}
    entidad = str(contrato.get("entidad") or "Sin Entidad")
    numero_contrato = str(contrato.get("numero_contrato") or "N/A")
    mes = int(contrato.get("mes") or 1)
    anio = int(contrato.get("anio") or 2025)

    try:
        adapter = DriveAdapter(db)
        path = build_contract_drive_path(entidad, numero_contrato, anio, mes)
        folder_id = await adapter.get_or_create_folder(user_id, path)

        drive_file = await adapter.upload_file(
            usuario_id=user_id,
            name=pdf_filename,
            content=pdf_bytes,
            mime_type="application/pdf",
            folder_id=folder_id,
        )

        await logger.ainfo(
            "drive_upload_complete",
            user_id=str(user_id),
            file_id=drive_file.id,
            folder_path="/".join(path),
        )

        response = (
            f"El PDF **{pdf_filename}** fue subido exitosamente a Google Drive.\n"
            f"Carpeta: `{' / '.join(path)}`\n"
            f"[Ver archivo en Drive]({drive_file.web_view_link})"
        )

        return {
            **state,
            "drive_folder_id": folder_id,
            "drive_file_ids": [drive_file.id],
            "drive_share_links": [],
            "response": response,
        }

    except Exception as exc:
        await logger.aerror("drive_upload_error", error=str(exc), user_id=str(user_id))
        return {
            **state,
            "error": str(exc),
            "response": f"Error subiendo a Drive: {exc}. Verifica que tu cuenta de Google esté conectada.",
        }
