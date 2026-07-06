"""Folder organizer node — creates organized folder structure in S3/Drive (Phase 5)."""

from __future__ import annotations

import re
from datetime import datetime

import structlog

from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.folder_organizer")


def _slugify(text: str) -> str:
    """Convert text to folder-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[áàä]", "a", text)
    text = re.sub(r"[éèë]", "e", text)
    text = re.sub(r"[íìï]", "i", text)
    text = re.sub(r"[óòö]", "o", text)
    text = re.sub(r"[úùü]", "u", text)
    text = re.sub(r"[ñ]", "n", text)
    text = re.sub(r"[_\s]+", "-", text)
    text = re.sub(r"[^a-z0-9-]", "", text)
    return text[:40].strip("-")


def _build_paths(
    entidad: str,
    numero_contrato: str,
    mes: int,
    anio: int,
    doc_types: list[str],
) -> dict[str, str]:
    """Build folder paths following {entidad_slug}/{ref_contrato}/{YYYY-MM}/{tipo_doc}/."""
    entidad_slug = _slugify(entidad) or "entidad-desconocida"
    ref_slug = _slugify(numero_contrato) or "contrato"
    periodo = f"{anio:04d}-{mes:02d}" if anio and mes else datetime.utcnow().strftime("%Y-%m")

    manifest: dict[str, str] = {}
    for dtype in doc_types:
        dtype_slug = _slugify(dtype) or "documento"
        path = f"{entidad_slug}/{ref_slug}/{periodo}/{dtype_slug}/"
        manifest[dtype] = path

    return manifest


async def folder_organizer_node(state: AgentState) -> AgentState:
    """Build folder structure manifest for all generated documents.

    Reads: document_drafts, contrato_extraido, mes, anio
    Writes: folder_manifest, current_phase
    """
    drafts: list[dict] = state.get("document_drafts") or []
    contrato: dict = state.get("contrato_extraido") or {}
    mes: int = state.get("mes") or 0
    anio: int = state.get("anio") or 0

    entidad = contrato.get("entidad") or contrato.get("nombre_entidad") or "entidad"
    numero = contrato.get("numero_contrato") or "contrato"

    # Collect unique doc types
    doc_types = list({d.get("type", "documento") for d in drafts}) if drafts else ["cuenta_cobro"]

    manifest = _build_paths(entidad, numero, mes, anio, doc_types)

    await logger.ainfo(
        "folder_organizer_done",
        entidad=entidad,
        numero=numero,
        periodo=f"{anio}-{mes:02d}",
        n_paths=len(manifest),
    )

    return {
        **state,
        "folder_manifest": manifest,
        "current_phase": "folder_organizer",
    }
