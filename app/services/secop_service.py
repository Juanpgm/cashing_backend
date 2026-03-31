"""Service for SECOP public contracting data integration (datos.gov.co)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ExternalServiceError, ValidationError
from app.models.secop import SecopContrato, SecopDocumento, SecopProceso
from app.schemas.secop import (
    SecopConsultaCompletaResponse,
    SecopContratoDetalleResponse,
    SecopContratoResponse,
    SecopDocumentoResponse,
    SecopProcesoResponse,
)

log = structlog.get_logger("service.secop")

_SECOP_BASE = "https://www.datos.gov.co/api/v3/views"
_DS_CONTRATOS = "jbjy-vk9h"
_DS_PROCESOS = "p6dx-8zbt"
_DS_DOCUMENTOS = "dmgg-8hin"
_CACHE_TTL = timedelta(hours=24)
_PRESTACION = "prestaci"  # substring present in all "Prestación de Servicios" variants


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.split("T")[0]).date()
    except (ValueError, AttributeError):
        return None


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _is_prestacion_servicios(tipo: str | None) -> bool:
    return bool(tipo and _PRESTACION in tipo.lower())


def _is_fresh(updated_at: datetime) -> bool:
    now = datetime.now(tz=UTC)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return (now - updated_at) < _CACHE_TTL


async def _query_socrata(dataset_id: str, where_clause: str, limit: int = 500) -> list[dict[str, Any]]:
    """Execute a SoQL query against a datos.gov.co dataset."""
    url = f"{_SECOP_BASE}/{dataset_id}/query.json"
    soql = f"SELECT * WHERE {where_clause} LIMIT {limit}"
    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params={"$query": soql}, headers=headers)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        log.error("secop_api_http_error", dataset=dataset_id, status=exc.response.status_code)
        raise ExternalServiceError("SECOP API", f"HTTP {exc.response.status_code}") from exc
    except httpx.RequestError as exc:
        log.error("secop_api_request_error", dataset=dataset_id, error=str(exc))
        raise ExternalServiceError("SECOP API", "connection error") from exc

    data: Any = response.json()
    if isinstance(data, list):
        return data
    results: list[dict[str, Any]] = data.get("results", [])
    return results


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


async def _upsert_contrato(db: AsyncSession, row: dict[str, Any]) -> SecopContrato | None:
    id_secop = str(row.get("id_contrato") or "").strip()
    if not id_secop:
        return None

    result = await db.execute(select(SecopContrato).where(SecopContrato.id_contrato_secop == id_secop))
    obj = result.scalar_one_or_none()

    fields: dict[str, Any] = {
        "id_contrato_secop": id_secop,
        "cedula_contratista": str(row.get("documento_proveedor") or "").strip(),
        "tipodocproveedor": row.get("tipodocproveedor"),
        "nombre_contratista": row.get("proveedor_adjudicado"),
        "nombre_entidad": row.get("nombre_entidad"),
        "nit_entidad": row.get("nit_entidad"),
        "sector": row.get("sector"),
        "departamento": row.get("departamento"),
        "ciudad": row.get("ciudad"),
        "proceso_de_compra": row.get("proceso_de_compra"),
        "numero_contrato": row.get("numero_contrato"),
        "referencia_del_contrato": row.get("referencia_del_contrato"),
        "tipo_de_contrato": row.get("tipo_de_contrato"),
        "modalidad_de_contratacion": row.get("modalidad_de_contratacion"),
        "descripcion_del_proceso": row.get("descripcion_del_proceso"),
        "estado_contrato": row.get("estado_contrato"),
        "fecha_de_firma": _parse_date(row.get("fecha_de_firma")),
        "fecha_inicio": _parse_date(row.get("fecha_de_inicio_del_contrato")),
        "fecha_fin": _parse_date(row.get("fecha_de_fin_del_contrato")),
        "valor_del_contrato": _parse_float(row.get("valor_del_contrato")),
        "valor_pagado": _parse_float(row.get("valor_pagado")),
        "datos_raw": row,
    }

    if obj is None:
        obj = SecopContrato(**fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)

    await db.flush()
    return obj


async def _upsert_proceso(db: AsyncSession, row: dict[str, Any]) -> SecopProceso | None:
    id_secop = str(row.get("id_del_proceso") or "").strip()
    if not id_secop:
        return None

    result = await db.execute(select(SecopProceso).where(SecopProceso.id_proceso_secop == id_secop))
    obj = result.scalar_one_or_none()

    fields: dict[str, Any] = {
        "id_proceso_secop": id_secop,
        "referencia_del_proceso": row.get("referencia_del_proceso"),
        "nombre_del_procedimiento": row.get("nombre_del_procedimiento"),
        "descripcion": row.get("descripci_n_del_procedimiento"),
        "entidad": row.get("entidad"),
        "nit_entidad": row.get("nit_entidad"),
        "departamento_entidad": row.get("departamento_entidad"),
        "ciudad_entidad": row.get("ciudad_entidad"),
        "fase": row.get("fase"),
        "modalidad_de_contratacion": row.get("modalidad_de_contratacion"),
        "precio_base": _parse_float(row.get("precio_base")),
        "estado_del_procedimiento": row.get("estado_del_procedimiento"),
        "fecha_de_publicacion": _parse_date(row.get("fecha_de_publicacion_del")),
        "adjudicado": str(row.get("adjudicado") or ""),
        "duracion": str(row.get("duracion") or "") or None,
        "unidad_de_duracion": row.get("unidad_de_duracion"),
        "datos_raw": row,
    }

    if obj is None:
        obj = SecopProceso(**fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)

    await db.flush()
    return obj


async def _upsert_documento(db: AsyncSession, row: dict[str, Any]) -> SecopDocumento | None:
    id_secop = str(row.get("id_documento") or "").strip()
    if not id_secop:
        return None

    result = await db.execute(select(SecopDocumento).where(SecopDocumento.id_documento_secop == id_secop))
    obj = result.scalar_one_or_none()

    fields: dict[str, Any] = {
        "id_documento_secop": id_secop,
        "numero_contrato": row.get("n_mero_de_contrato"),
        "proceso": row.get("proceso"),
        "nombre_archivo": row.get("nombre_archivo"),
        "tamanno_archivo": str(row.get("tamanno_archivo") or "") or None,
        "extension": row.get("extensi_n"),
        "descripcion": row.get("descripci_n"),
        "fecha_carga": _parse_date(row.get("fecha_carga")),
        "entidad": row.get("entidad"),
        "nit_entidad": row.get("nit_entidad"),
        "url_descarga": row.get("url_descarga_documento"),
        "datos_raw": row,
    }

    if obj is None:
        obj = SecopDocumento(**fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)

    await db.flush()
    return obj


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def buscar_contratos_cedula(
    db: AsyncSession,
    cedula: str,
    refresh: bool = False,
) -> list[SecopContratoResponse]:
    """Fetch contracts for a cedula from cache (or Socrata if stale/refresh)."""
    if not re.match(r"^\d{5,15}$", cedula):
        raise ValidationError("La cédula debe contener entre 5 y 15 dígitos")

    result = await db.execute(select(SecopContrato).where(SecopContrato.cedula_contratista == cedula))
    cached = result.scalars().all()

    needs_refresh = refresh or not cached or (cached and not _is_fresh(cached[0].updated_at))

    if needs_refresh:
        rows = await _query_socrata(
            _DS_CONTRATOS,
            where_clause=f"documento_proveedor = '{cedula}'",
            limit=500,
        )
        log.info("secop_contratos_fetched", cedula=cedula, count=len(rows))
        for row in rows:
            await _upsert_contrato(db, row)
        await db.commit()

        result = await db.execute(select(SecopContrato).where(SecopContrato.cedula_contratista == cedula))
        cached = result.scalars().all()

    return [SecopContratoResponse.model_validate(c) for c in cached if _is_prestacion_servicios(c.tipo_de_contrato)]


async def obtener_proceso(
    db: AsyncSession,
    id_proceso: str,
    refresh: bool = False,
) -> SecopProcesoResponse | None:
    """Fetch a procurement process by its SECOP ID."""
    result = await db.execute(select(SecopProceso).where(SecopProceso.id_proceso_secop == id_proceso))
    cached = result.scalar_one_or_none()

    if cached is None or refresh or not _is_fresh(cached.updated_at):
        rows = await _query_socrata(
            _DS_PROCESOS,
            where_clause=f"id_del_proceso = '{id_proceso}'",
            limit=1,
        )
        if not rows:
            return None
        cached = await _upsert_proceso(db, rows[0])
        await db.commit()

    return SecopProcesoResponse.model_validate(cached) if cached else None


async def buscar_documentos_contrato(
    db: AsyncSession,
    numero_contrato: str,
    refresh: bool = False,
) -> list[SecopDocumentoResponse]:
    """Fetch documents for a contract number."""
    result = await db.execute(select(SecopDocumento).where(SecopDocumento.numero_contrato == numero_contrato))
    cached = result.scalars().all()

    needs_refresh = refresh or not cached or (cached and not _is_fresh(cached[0].updated_at))

    if needs_refresh:
        safe_num = numero_contrato.replace("'", "''")
        rows = await _query_socrata(
            _DS_DOCUMENTOS,
            where_clause=f"n_mero_de_contrato = '{safe_num}'",
            limit=100,
        )
        for row in rows:
            await _upsert_documento(db, row)
        if rows:
            await db.commit()
            result = await db.execute(select(SecopDocumento).where(SecopDocumento.numero_contrato == numero_contrato))
            cached = result.scalars().all()

    return [SecopDocumentoResponse.model_validate(d) for d in cached]


async def consulta_completa(
    db: AsyncSession,
    cedula: str,
    refresh: bool = False,
) -> SecopConsultaCompletaResponse:
    """Full query: contracts + associated process + documents for a cedula."""
    contratos = await buscar_contratos_cedula(db, cedula, refresh=refresh)

    async def _enriquecer(contrato: SecopContratoResponse) -> SecopContratoDetalleResponse:
        async def _none() -> None:
            return None

        async def _empty() -> list[SecopDocumentoResponse]:
            return []

        proceso_coro = (
            obtener_proceso(db, contrato.proceso_de_compra, refresh=refresh) if contrato.proceso_de_compra else _none()
        )
        docs_coro = (
            buscar_documentos_contrato(db, contrato.numero_contrato, refresh=refresh)
            if contrato.numero_contrato
            else _empty()
        )
        proceso = await proceso_coro
        docs = await docs_coro
        return SecopContratoDetalleResponse(contrato=contrato, proceso=proceso, documentos=docs or [])

    detalles = []
    for c in contratos:
        detalle = await _enriquecer(c)
        detalles.append(detalle)

    return SecopConsultaCompletaResponse(
        cedula=cedula,
        total_contratos=len(contratos),
        contratos=detalles,
    )
