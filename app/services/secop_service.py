"""Service for SECOP public contracting data integration (datos.gov.co)."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ExternalServiceError, ValidationError
from app.models.contrato import Contrato
from app.models.secop import SecopContrato, SecopDocumento, SecopProceso
from app.schemas.contrato import ContratoCreate, ContratoResponse
from app.schemas.secop import (
    SecopConsultaCompletaResponse,
    SecopContratoDetalleResponse,
    SecopContratoResponse,
    SecopDocumentoResponse,
    SecopImportResult,
    SecopProcesoResponse,
    SecopSincronizarDocumentosResult,
)

log = structlog.get_logger("service.secop")

_SECOP_BASE = "https://www.datos.gov.co/resource"
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
    """Execute a Socrata REST query against a datos.gov.co dataset."""
    url = f"{_SECOP_BASE}/{dataset_id}.json"
    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}
    params = {"$where": where_clause, "$limit": str(limit)}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, params=params, headers=headers)
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
        # numero_contrato is often NULL in SECOP; fallback to referencia_del_contrato
        # (CO1.PCCNTR.xxx format) which is the key that links to documentos dataset
        "numero_contrato": row.get("numero_contrato") or row.get("referencia_del_contrato"),
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
    # Integration key: proceso_de_compra in contratos matches id_del_portafolio in procesos
    id_secop = str(row.get("id_del_portafolio") or "").strip()
    if not id_secop:
        return None

    result = await db.execute(select(SecopProceso).where(SecopProceso.id_proceso_secop == id_secop))
    obj = result.scalar_one_or_none()

    fields: dict[str, Any] = {
        "id_proceso_secop": id_secop,  # = id_del_portafolio (CO1.BDOS.xxx)
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


async def _upsert_documento(
    db: AsyncSession,
    row: dict[str, Any],
    secop_contrato_id: uuid.UUID | None = None,
    secop_proceso_id: uuid.UUID | None = None,
) -> SecopDocumento | None:
    id_secop = str(row.get("id_documento") or "").strip()
    if not id_secop:
        return None

    result = await db.execute(select(SecopDocumento).where(SecopDocumento.id_documento_secop == id_secop))
    obj = result.scalar_one_or_none()

    fields: dict[str, Any] = {
        "id_documento_secop": id_secop,
        "numero_contrato": row.get("n_mero_de_contrato"),
        "proceso": row.get("proceso"),
        "secop_contrato_id": secop_contrato_id,
        "secop_proceso_id": secop_proceso_id,
        "nombre_archivo": row.get("nombre_archivo"),
        "tamanno_archivo": str(row.get("tamanno_archivo") or "") or None,
        "extension": row.get("extensi_n"),
        "descripcion": row.get("descripci_n"),
        "fecha_carga": _parse_date(row.get("fecha_carga")),
        "entidad": row.get("entidad"),
        "nit_entidad": row.get("nit_entidad"),
        "url_descarga": (row.get("url_descarga_documento") or {}).get("url")
        if isinstance(row.get("url_descarga_documento"), dict)
        else row.get("url_descarga_documento"),
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
            where_clause=f"id_del_portafolio = '{id_proceso}'",
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
    """Fetch ALL documents for a contract by both numero_contrato and proceso_de_compra.

    SECOP documents are linked to contracts via two different keys:
    - n_mero_de_contrato (CO1.PCCNTR.xxx) — direct contract docs
    - proceso (CO1.BDOS.xxx) — process docs, often contain the contract minute / obligations
    Both sets are merged and deduplicated to return the full document set.
    """
    # Resolve SecopContrato → get proceso_de_compra and FK id
    contrato_result = await db.execute(
        select(SecopContrato).where(SecopContrato.numero_contrato == numero_contrato)
    )
    secop_contrato = contrato_result.scalar_one_or_none()
    contrato_id = secop_contrato.id if secop_contrato else None
    proceso_de_compra = secop_contrato.proceso_de_compra if secop_contrato else None

    # Resolve SecopProceso FK if we have proceso_de_compra
    proceso_id: uuid.UUID | None = None
    if proceso_de_compra:
        proceso_result = await db.execute(
            select(SecopProceso).where(SecopProceso.id_proceso_secop == proceso_de_compra)
        )
        secop_proceso = proceso_result.scalar_one_or_none()
        proceso_id = secop_proceso.id if secop_proceso else None

    # Check cache: docs linked by either key
    cache_conditions = [SecopDocumento.numero_contrato == numero_contrato]
    if proceso_de_compra:
        cache_conditions.append(SecopDocumento.proceso == proceso_de_compra)
    cached_result = await db.execute(select(SecopDocumento).where(or_(*cache_conditions)))
    cached = cached_result.scalars().all()

    needs_refresh = refresh or not cached or (cached and not _is_fresh(cached[0].updated_at))

    if needs_refresh:
        safe_num = numero_contrato.replace("'", "''")

        # Query 1: docs linked directly by numero_contrato (CO1.PCCNTR.xxx)
        rows_by_num = await _query_socrata(
            _DS_DOCUMENTOS,
            where_clause=f"n_mero_de_contrato = '{safe_num}'",
            limit=200,
        )

        # Query 2: docs linked by proceso_de_compra (CO1.BDOS.xxx) — includes contract minute
        rows_by_proceso: list[dict[str, Any]] = []
        if proceso_de_compra:
            safe_proceso = proceso_de_compra.replace("'", "''")
            rows_by_proceso = await _query_socrata(
                _DS_DOCUMENTOS,
                where_clause=f"proceso = '{safe_proceso}'",
                limit=200,
            )

        # Merge and deduplicate by id_documento
        seen: set[str] = set()
        for row in rows_by_num:
            id_doc = str(row.get("id_documento") or "").strip()
            if id_doc and id_doc not in seen:
                seen.add(id_doc)
                await _upsert_documento(db, row, secop_contrato_id=contrato_id, secop_proceso_id=proceso_id)
        for row in rows_by_proceso:
            id_doc = str(row.get("id_documento") or "").strip()
            if id_doc and id_doc not in seen:
                seen.add(id_doc)
                await _upsert_documento(db, row, secop_contrato_id=contrato_id, secop_proceso_id=proceso_id)

        if seen:
            await db.commit()
            cached_result = await db.execute(select(SecopDocumento).where(or_(*cache_conditions)))
            cached = cached_result.scalars().all()

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


# ---------------------------------------------------------------------------
# Importar contratos SECOP → tabla contratos del usuario
# ---------------------------------------------------------------------------


def _calcular_valor_mensual(valor_total: Decimal, fecha_inicio: date, fecha_fin: date) -> Decimal:
    dias = (fecha_fin - fecha_inicio).days
    meses = max(1, round(dias / 30))
    try:
        return (valor_total / Decimal(meses)).quantize(Decimal("0.01"))
    except (InvalidOperation, ZeroDivisionError):
        return valor_total


def _mapear_a_contrato_create(row: dict[str, Any]) -> ContratoCreate | None:
    """Map a raw SECOP row to ContratoCreate. Returns None if data is insufficient."""
    # --- numero_contrato ---
    numero = (
        str(row.get("numero_contrato") or "").strip()
        or str(row.get("referencia_del_contrato") or "").strip()
        or str(row.get("id_contrato") or "").strip()
    )[:100]
    if not numero:
        return None

    # --- objeto ---
    objeto = (
        str(row.get("objeto_del_contrato") or "").strip()
        or str(row.get("descripcion_del_proceso") or "").strip()
    )[:2000]
    if len(objeto) < 10:
        return None

    # --- valor_total ---
    try:
        valor_total = Decimal(str(row.get("valor_del_contrato") or 0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if valor_total <= 0:
        return None

    # --- fechas ---
    fecha_inicio = _parse_date(row.get("fecha_de_inicio_del_contrato"))
    fecha_fin = _parse_date(row.get("fecha_de_fin_del_contrato"))
    if not fecha_inicio or not fecha_fin or fecha_fin <= fecha_inicio:
        return None

    # --- valor_mensual (calculado) ---
    valor_mensual = _calcular_valor_mensual(valor_total, fecha_inicio, fecha_fin)

    # --- campos opcionales ---
    supervisor = (str(row.get("nombre_supervisor") or "").strip() or None)
    if supervisor:
        supervisor = supervisor[:255]

    entidad = (str(row.get("nombre_entidad") or "").strip() or None)
    if entidad:
        entidad = entidad[:255]

    return ContratoCreate(
        numero_contrato=numero,
        objeto=objeto,
        valor_total=valor_total,
        valor_mensual=valor_mensual,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        supervisor_nombre=supervisor,
        entidad=entidad,
        dependencia=None,
        obligaciones=[],
    )


async def importar_contratos_secop(
    db: AsyncSession,
    documento_proveedor: str,
    usuario_id: uuid.UUID,
    confirmar: bool = True,
) -> SecopImportResult:
    """Fetch all SECOP contracts for a documento_proveedor and persist them
    into the user's contratos table, skipping duplicates and invalid rows."""
    if not re.match(r"^\d{5,15}$", documento_proveedor):
        raise ValidationError("El documento_proveedor debe contener entre 5 y 15 dígitos")

    rows = await _query_socrata(
        _DS_CONTRATOS,
        where_clause=f"documento_proveedor = '{documento_proveedor}'",
        limit=500,
    )
    log.info("secop_importar_fetched", documento_proveedor=documento_proveedor, total=len(rows))

    # Cache in secop_contratos as side-effect
    for row in rows:
        await _upsert_contrato(db, row)

    # Load existing numero_contrato for this user to detect duplicates
    existing_result = await db.execute(
        select(Contrato.numero_contrato).where(
            Contrato.usuario_id == usuario_id,
            Contrato.deleted_at.is_(None),
        )
    )
    existing_numeros = {r[0] for r in existing_result.all()}

    importados: list[ContratoResponse] = []
    omitidos_duplicados = 0
    omitidos_invalidos = 0

    from sqlalchemy.orm import selectinload

    for row in rows:
        data = _mapear_a_contrato_create(row)
        if data is None:
            omitidos_invalidos += 1
            continue

        if data.numero_contrato in existing_numeros:
            omitidos_duplicados += 1
            continue

        if not confirmar:
            # Preview mode: build response without persisting
            importados.append(ContratoResponse(
                id=uuid.uuid4(),
                usuario_id=usuario_id,
                numero_contrato=data.numero_contrato,
                objeto=data.objeto,
                valor_total=data.valor_total,
                valor_mensual=data.valor_mensual,
                fecha_inicio=data.fecha_inicio,
                fecha_fin=data.fecha_fin,
                supervisor_nombre=data.supervisor_nombre,
                entidad=data.entidad,
                dependencia=data.dependencia,
                documento_proveedor=documento_proveedor,
                obligaciones=[],
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ))
            existing_numeros.add(data.numero_contrato)
            continue

        contrato = Contrato(
            usuario_id=usuario_id,
            numero_contrato=data.numero_contrato,
            objeto=data.objeto,
            valor_total=data.valor_total,
            valor_mensual=data.valor_mensual,
            fecha_inicio=data.fecha_inicio,
            fecha_fin=data.fecha_fin,
            supervisor_nombre=data.supervisor_nombre,
            entidad=data.entidad,
            dependencia=data.dependencia,
            documento_proveedor=documento_proveedor,
        )
        db.add(contrato)
        await db.flush()
        existing_numeros.add(data.numero_contrato)

        result = await db.execute(
            select(Contrato)
            .options(selectinload(Contrato.obligaciones))
            .where(Contrato.id == contrato.id)
        )
        importados.append(ContratoResponse.model_validate(result.scalar_one()))

    if confirmar:
        await db.commit()
    log.info(
        "secop_importar_done",
        documento_proveedor=documento_proveedor,
        importados=len(importados),
        omitidos_duplicados=omitidos_duplicados,
        omitidos_invalidos=omitidos_invalidos,
    )

    return SecopImportResult(
        documento_proveedor=documento_proveedor,
        encontrados_en_secop=len(rows),
        importados=len(importados),
        omitidos_duplicados=omitidos_duplicados,
        omitidos_invalidos=omitidos_invalidos,
        contratos=importados,
    )


# ---------------------------------------------------------------------------
# Sincronizar documentos para todos los contratos y procesos cacheados
# ---------------------------------------------------------------------------


async def sincronizar_documentos_secop(
    db: AsyncSession,
    cedula: str,
    confirmar: bool = False,
) -> SecopSincronizarDocumentosResult:
    """Fetch and link all SECOP documents for every cached contrato and proceso of a cedula.

    confirmar=False → preview only, shows what would be saved without persisting.
    confirmar=True  → saves documents to DB linked to their contrato/proceso.
    """
    if not re.match(r"^\d{5,15}$", cedula):
        raise ValidationError("La cédula debe contener entre 5 y 15 dígitos")

    # Load all cached contratos for this cedula
    contratos_result = await db.execute(
        select(SecopContrato).where(SecopContrato.cedula_contratista == cedula)
    )
    contratos = contratos_result.scalars().all()

    # Load all cached procesos via proceso_de_compra links
    proceso_ids = {c.proceso_de_compra for c in contratos if c.proceso_de_compra}
    procesos: list[SecopProceso] = []
    if proceso_ids:
        procesos_result = await db.execute(
            select(SecopProceso).where(SecopProceso.id_proceso_secop.in_(list(proceso_ids)))
        )
        procesos = procesos_result.scalars().all()

    # Track existing document IDs to detect duplicates
    existing_ids_result = await db.execute(select(SecopDocumento.id_documento_secop))
    existing_ids = {r[0] for r in existing_ids_result.all()}

    all_docs: list[SecopDocumento] = []
    docs_guardados = 0
    docs_omitidos = 0

    # Fetch documents for each contrato
    for contrato in contratos:
        if not contrato.numero_contrato:
            continue
        safe_num = contrato.numero_contrato.replace("'", "''")
        rows = await _query_socrata(
            _DS_DOCUMENTOS,
            where_clause=f"n_mero_de_contrato = '{safe_num}'",
            limit=100,
        )
        for row in rows:
            id_doc = str(row.get("id_documento") or "").strip()
            if not id_doc:
                continue
            if id_doc in existing_ids:
                docs_omitidos += 1
                # Build a temp response for preview
                tmp = SecopDocumento(
                    id_documento_secop=id_doc,
                    numero_contrato=row.get("n_mero_de_contrato"),
                    proceso=row.get("proceso"),
                    secop_contrato_id=contrato.id,
                    nombre_archivo=row.get("nombre_archivo"),
                    extension=row.get("extensi_n"),
                    descripcion=row.get("descripci_n"),
                    url_descarga=(row.get("url_descarga_documento") or {}).get("url")
                    if isinstance(row.get("url_descarga_documento"), dict)
                    else row.get("url_descarga_documento"),
                    dados_raw=row,
                )
                all_docs.append(tmp)
                continue
            if confirmar:
                doc = await _upsert_documento(db, row, secop_contrato_id=contrato.id)
                if doc:
                    all_docs.append(doc)
                    existing_ids.add(id_doc)
                    docs_guardados += 1
            else:
                # Preview: build in-memory object
                all_docs.append(SecopDocumento(
                    id_documento_secop=id_doc,
                    numero_contrato=row.get("n_mero_de_contrato"),
                    proceso=row.get("proceso"),
                    secop_contrato_id=contrato.id,
                    nombre_archivo=row.get("nombre_archivo"),
                    extension=row.get("extensi_n"),
                    descripcion=row.get("descripci_n"),
                    url_descarga=(row.get("url_descarga_documento") or {}).get("url")
                    if isinstance(row.get("url_descarga_documento"), dict)
                    else row.get("url_descarga_documento"),
                ))
                existing_ids.add(id_doc)
                docs_guardados += 1

    # Fetch documents for each proceso
    for proceso in procesos:
        safe_proc = proceso.id_proceso_secop.replace("'", "''")
        rows = await _query_socrata(
            _DS_DOCUMENTOS,
            where_clause=f"proceso = '{safe_proc}'",
            limit=100,
        )
        for row in rows:
            id_doc = str(row.get("id_documento") or "").strip()
            if not id_doc or id_doc in existing_ids:
                docs_omitidos += 1
                continue
            if confirmar:
                doc = await _upsert_documento(db, row, secop_proceso_id=proceso.id)
                if doc:
                    all_docs.append(doc)
                    existing_ids.add(id_doc)
                    docs_guardados += 1
            else:
                all_docs.append(SecopDocumento(
                    id_documento_secop=id_doc,
                    numero_contrato=row.get("n_mero_de_contrato"),
                    proceso=row.get("proceso"),
                    secop_proceso_id=proceso.id,
                    nombre_archivo=row.get("nombre_archivo"),
                    extension=row.get("extensi_n"),
                    descripcion=row.get("descripci_n"),
                    url_descarga=(row.get("url_descarga_documento") or {}).get("url")
                    if isinstance(row.get("url_descarga_documento"), dict)
                    else row.get("url_descarga_documento"),
                ))
                existing_ids.add(id_doc)
                docs_guardados += 1

    if confirmar:
        await db.commit()

    log.info(
        "secop_sincronizar_docs",
        cedula=cedula,
        contratos=len(contratos),
        procesos=len(procesos),
        guardados=docs_guardados,
        omitidos=docs_omitidos,
        confirmar=confirmar,
    )

    # Build response — in-memory objects don't have .id, so use placeholder UUID
    docs_response: list[SecopDocumentoResponse] = []
    for d in all_docs:
        docs_response.append(SecopDocumentoResponse(
            id=getattr(d, "id", None) or uuid.uuid4(),
            id_documento_secop=d.id_documento_secop,
            numero_contrato=d.numero_contrato,
            proceso=d.proceso,
            secop_contrato_id=d.secop_contrato_id,
            secop_proceso_id=d.secop_proceso_id,
            nombre_archivo=d.nombre_archivo,
            extension=d.extension,
            descripcion=d.descripcion,
            fecha_carga=d.fecha_carga if hasattr(d, "fecha_carga") else None,
            entidad=d.entidad if hasattr(d, "entidad") else None,
            nit_entidad=d.nit_entidad if hasattr(d, "nit_entidad") else None,
            url_descarga=d.url_descarga,
            updated_at=d.updated_at if hasattr(d, "updated_at") and d.updated_at else datetime.now(tz=UTC),
        ))

    return SecopSincronizarDocumentosResult(
        contratos_procesados=len(contratos),
        procesos_procesados=len(procesos),
        documentos_encontrados=len(all_docs),
        documentos_guardados=docs_guardados,
        documentos_omitidos_duplicados=docs_omitidos,
        confirmar=confirmar,
        documentos=docs_response,
    )
