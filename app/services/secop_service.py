"""Service for SECOP public contracting data integration (datos.gov.co)."""

from __future__ import annotations

import asyncio
import io
import random
import re
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.contract_parser import extract_obligaciones_verbatim
from app.core.config import settings
from app.core.exceptions import ExternalServiceError, NotFoundError, ValidationError
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.secop import SecopContrato, SecopDocumento, SecopProceso
from app.schemas.contrato import ContratoCreate, ContratoResponse, ObligacionCreate
from app.schemas.secop import (
    ArchivoComprimidoResponse,
    ArchivoInternoItem,
    CoberturaDocumentosInfo,
    SecopConfiguracionResponse,
    SecopConsultaCompletaResponse,
    SecopContratoDetalleResponse,
    SecopContratoImportado,
    SecopContratoResponse,
    SecopDocumentoResponse,
    SecopDocumentosConCoberturaResponse,
    SecopEstadoDataset,
    SecopEstadoDatasetsResponse,
    SecopImportResult,
    SecopProcesoResponse,
    SecopSincronizarDocumentosResult,
)

log = structlog.get_logger("service.secop")

_SECOP_BASE = "https://www.datos.gov.co/resource"
_DS_CONTRATOS = "jbjy-vk9h"
_DS_PROCESOS = "p6dx-8zbt"
# Archive document datasets (one per year-range)
_DS_DOCS_HIST = "f8va-cf4m"  # hasta 31/12/2021 (cubre 2018-2021)
_DS_DOCS_2022 = "kgcd-kt7i"
_DS_DOCS_2023 = "3skv-9na7"
# dmgg-8hin's bulk coverage starts 01/01/2025, BUT it is NOT gap-free for 2024:
# a live probe (2026-07-11, see openspec/changes/secop-full-acquisition/tasks.md
# Slice 1 task 1.2) found it also holds a single 2024-12-31 bulk-load batch
# (~24.5k rows, all sharing that exact fecha_carga), not full-year 2024
# coverage. _DS_DOCS_2023 (3skv-9na7) returned 0 rows for 2024 in the same
# probe. Net effect: most of 2024 (Jan-Nov) still has no covering dataset —
# the secop_docs_gap_2024 warning below stays in place for that reason.
_DS_DOCS_2025 = "dmgg-8hin"  # desde 01/01/2025 + one 2024-12-31 bulk-load batch (not full 2024 coverage)
_DS_DOCUMENTOS = _DS_DOCS_2025  # backward-compat alias used by sincronizar_documentos
_DS_MODIFICACIONES = "u8cx-r425"  # SECOP II Modificaciones a contratos

# Slice 2 (secop-dataset-ingestion): Adiciones / Modificaciones a Procesos / Ubicaciones.
_DS_ADICIONES = "cb9c-h8sn"  # SECOP II Modificaciones a Contratos (filtered to tipo="ADICION EN EL VALOR")
_DS_MODIF_PROCESOS = "e2u2-swiw"  # SECOP II Modificaciones a Procesos
_DS_UBICACIONES = "gra4-pcp2"  # SECOP II Ubicaciones ejecucion contratos

# Live schema probe (2026-07-11, tasks.md Slice 2 task 2.1) — DEVIATION from the
# design/spec join-key assumptions, corrected here per D3's "act on the probe's
# result":
#   - cb9c-h8sn: `id_contrato` confirmed present, as assumed. BUT the dataset has
#     NO structured value field (only identificador/id_contrato/tipo/descripcion/
#     fecharegistro) — `valor_adicion` must be best-effort parsed from free-text
#     `descripcion` (see `_extraer_valor_adicion_texto`), and rows must be filtered
#     to tipo == "ADICION EN EL VALOR" (confirmed live) to mean "Adición" at all.
#   - e2u2-swiw: real field names are `portafolio` and `proceso`, NOT
#     `proceso_de_compra`/`id_del_portafolio` as originally assumed. `portafolio`
#     (format CO1.BDOS.xxx) is what matches our `proceso_de_compra`/
#     `id_proceso_secop` values — the query VALUE still falls back between those
#     two of our own fields, it is the SOCRATA FIELD NAME that was wrong, not the
#     fallback concept. Also: per its own Socrata description, this dataset only
#     contains processes modified in the "últimos 8 días" (rolling 8-day window) —
#     most historical contracts legitimately return zero rows here; this is
#     expected, not a gap.
#   - gra4-pcp2: `referencia_del_contrato` confirmed present, matches design as-is.
_DATASET_JOIN_KEYS: dict[str, tuple[str, ...]] = {
    _DS_ADICIONES: ("id_contrato",),
    _DS_MODIF_PROCESOS: ("portafolio", "proceso"),
    _DS_UBICACIONES: ("referencia_del_contrato",),
}

_ALL_DOCS_DATASETS = (_DS_DOCS_HIST, _DS_DOCS_2022, _DS_DOCS_2023, _DS_DOCS_2025)
_CACHE_TTL = timedelta(hours=24)
_CACHE_TTL_DOCS = timedelta(hours=2)  # Documents refresh more frequently
_PRESTACION = "prestaci"  # substring present in all "Prestación de Servicios" variants

# Bounded retry/backoff for _query_socrata (secop-acquisition-resilience spec,
# design D5): Socrata throttles hard on 429 and occasionally 5xx under load.
# Full jitter keeps concurrent fan-out callers from retrying in lockstep.
_SOCRATA_RETRY_MAX_ATTEMPTS = 3
_SOCRATA_RETRY_BASE_DELAY_S = 0.5

# The per-contract LLM obligaciones fallback (see importar_contratos_secop) can
# take minutes across retries and the fallback chain. Bound it so a bulk SECOP
# import can never hang or fail because of it: cap the wall-clock time per
# contract, and cap how many contracts in a single import run attempt it at
# all (the rest simply keep requiere_obligaciones=true for manual follow-up).
SECOP_OBLIGACIONES_LLM_TIMEOUT_S = 20
SECOP_OBLIGACIONES_LLM_MAX_CONTRATOS = 5


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


def _is_fresh(updated_at: datetime, ttl: timedelta = _CACHE_TTL) -> bool:
    now = datetime.now(tz=UTC)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return (now - updated_at) < ttl


async def _query_socrata(dataset_id: str, where_clause: str, limit: int = 500) -> list[dict[str, Any]]:
    """Execute a Socrata REST query against a datos.gov.co dataset.

    Retries on HTTP 429 and 5xx with bounded exponential backoff and full
    jitter (max `_SOCRATA_RETRY_MAX_ATTEMPTS` attempts) before giving up —
    Socrata throttles aggressively without an app token (see
    `SECOP_APP_TOKEN`) and transient 5xx responses happen under load. Other
    errors (4xx besides 429, connection errors) fail immediately, unchanged.
    """
    url = f"{_SECOP_BASE}/{dataset_id}.json"
    headers = {"X-App-Token": settings.SECOP_APP_TOKEN}
    params = {"$where": where_clause, "$limit": str(limit)}

    for attempt in range(1, _SOCRATA_RETRY_MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url, params=params, headers=headers)
                response.raise_for_status()
            data: Any = response.json()
            if isinstance(data, list):
                return data
            results: list[dict[str, Any]] = data.get("results", [])
            return results
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            retryable = status_code == 429 or status_code >= 500
            if retryable and attempt < _SOCRATA_RETRY_MAX_ATTEMPTS:
                delay = _SOCRATA_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)) * random.random()  # noqa: S311 — jitter, not crypto
                log.warning(
                    "secop_api_retry",
                    dataset=dataset_id,
                    status=status_code,
                    attempt=attempt,
                    delay_s=round(delay, 3),
                )
                await asyncio.sleep(delay)
                continue
            log.error("secop_api_http_error", dataset=dataset_id, status=status_code)
            raise ExternalServiceError("SECOP API", f"HTTP {status_code}") from exc
        except httpx.RequestError as exc:
            log.error("secop_api_request_error", dataset=dataset_id, error=str(exc))
            raise ExternalServiceError("SECOP API", "connection error") from exc

    # Unreachable: every loop iteration above either returns or raises.
    raise ExternalServiceError("SECOP API", "retry loop exhausted unexpectedly")


def _dataset_has_join_key(rows: list[dict[str, Any]], dataset_id: str) -> bool:
    """Schema-presence guard (D3, spec 'Schema verification before wiring any new
    dataset'). A dataset that returns zero rows is NOT schema drift — it may
    simply have no data for this contract (or, for `_DS_MODIF_PROCESOS`, be
    outside its rolling 8-day window). Only a dataset that DID return rows but
    none of them expose ANY of its expected join key(s) is treated as unavailable.
    """
    expected_keys = _DATASET_JOIN_KEYS.get(dataset_id, ())
    if not expected_keys or not rows:
        return True
    return any(any(key in row for key in expected_keys) for row in rows)


async def _query_dataset_guarded(
    dataset_id: str,
    where_clause: str,
    failed_out: list[str],
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query one of the schema-guarded datasets (Adiciones/ModifProcesos/Ubicaciones).

    Never raises: a query failure (retries exhausted) or a schema-drift result
    (rows present but missing the expected join key) both append `dataset_id` to
    `failed_out` and return an empty list — the caller composes this with the
    same partial-result contract used by `_query_docs_datasets`.
    """
    try:
        rows = await _query_socrata(dataset_id, where_clause, limit=limit)
    except ExternalServiceError as exc:
        log.warning("secop_nuevo_dataset_error", dataset=dataset_id, error=str(exc))
        failed_out.append(dataset_id)
        return []
    if not _dataset_has_join_key(rows, dataset_id):
        log.warning(
            "secop_nuevo_dataset_schema_drift",
            dataset=dataset_id,
            expected_keys=_DATASET_JOIN_KEYS.get(dataset_id, ()),
        )
        failed_out.append(dataset_id)
        return []
    return rows


# Best-effort peso-amount extraction from cb9c-h8sn's free-text `descripcion`
# (no structured value field exists — live probe finding, see _DS_ADICIONES note
# above). Matches "$1.234.567" style Colombian peso amounts; not every row has a
# parseable amount, in which case `valor_adicion` is None rather than guessed.
_VALOR_ADICION_RE = re.compile(r"\$\s?(\d{1,3}(?:[.,]\d{3})+)")


def _extraer_valor_adicion_texto(descripcion: str | None) -> Decimal | None:
    # Money never crosses this boundary as float — the accessor feeds
    # billing's contract-addition-events invoice math (CLAUDE.md anti-pattern).
    if not descripcion:
        return None
    match = _VALOR_ADICION_RE.search(descripcion)
    if not match:
        return None
    # Decimal-comma cents guard (task 4.5c): `_VALOR_ADICION_RE` only matches
    # complete thousands-separator groups (each exactly 3 digits). A value
    # written in Colombian decimal-comma notation, e.g. "$1.234,56" (=
    # 1234.56 pesos), has its trailing ",56" fragment (2 digits, not 3) left
    # UNCONSUMED right after the match — silently truncating to "1234" would
    # misreport the addition's value by dropping real cents. We choose to
    # REJECT (return None) rather than guess: in practice Colombian peso
    # contract additions are always whole-peso amounts, so a trailing decimal
    # fragment signals an unusual/ambiguous input better left for manual
    # review, consistent with this function's existing best-effort philosophy
    # ("valor_adicion is None rather than guessed").
    tail = descripcion[match.end() : match.end() + 3]
    if re.match(r"[.,]\d{1,2}(?!\d)", tail):
        return None
    digits = match.group(1).replace(".", "").replace(",", "")
    try:
        return Decimal(digits)
    except ArithmeticError:
        return None


async def _fetch_nuevos_datasets_contrato(
    id_contrato_secop: str | None,
    proceso_de_compra: str | None,
    id_del_portafolio: str | None,
    referencia_del_contrato: str | None,
    failed_out: list[str],
) -> dict[str, list[dict[str, Any]]]:
    """Fan out to the 3 new datasets for one contrato (+ its resolved proceso),
    each via its confirmed join key (spec 'Dataset-specific join semantics
    preserved'). A contract lacking a given dataset's key(s) is simply skipped
    for that dataset — zero rows, no error (spec 'No matching key for a
    contract')."""
    results: dict[str, list[dict[str, Any]]] = {
        "_adiciones": [],
        "_modif_procesos": [],
        "_ubicaciones": [],
    }

    coros: list[Any] = []
    result_keys: list[str] = []

    if id_contrato_secop:
        safe_id = id_contrato_secop.replace("'", "''")
        coros.append(
            _query_dataset_guarded(
                _DS_ADICIONES,
                f"id_contrato = '{safe_id}' AND tipo = 'ADICION EN EL VALOR'",
                failed_out,
            )
        )
        result_keys.append("_adiciones")

    # e2u2-swiw's real field is `portafolio` (see deviation note); prefer the
    # contrato's proceso_de_compra, fall back to the proceso's id_del_portafolio —
    # both are our own CO1.BDOS.xxx values, just sourced from different rows.
    portafolio_value = proceso_de_compra or id_del_portafolio
    if portafolio_value:
        safe_port = portafolio_value.replace("'", "''")
        coros.append(_query_dataset_guarded(_DS_MODIF_PROCESOS, f"portafolio = '{safe_port}'", failed_out))
        result_keys.append("_modif_procesos")

    if referencia_del_contrato:
        safe_ref = referencia_del_contrato.replace("'", "''")
        coros.append(_query_dataset_guarded(_DS_UBICACIONES, f"referencia_del_contrato = '{safe_ref}'", failed_out))
        result_keys.append("_ubicaciones")

    if coros:
        fetched = await asyncio.gather(*coros)
        for key, rows in zip(result_keys, fetched, strict=False):
            results[key] = rows

    return results


def _upsert_nuevos_datasets_raw(
    datos_raw: dict[str, Any] | None,
    nuevos: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, Any], bool]:
    """Non-destructive upsert (D1): only overwrite a reserved key when THIS
    refresh actually returned rows for it — an empty/failed refresh (whether
    zero rows or a schema/query error already filtered out by
    `_query_dataset_guarded`) leaves whatever was cached before untouched."""
    raw = dict(datos_raw or {})
    changed = False
    for key in ("_adiciones", "_ubicaciones"):
        rows = nuevos.get(key)
        if rows:
            raw[key] = rows
            changed = True
    return raw, changed


def upsert_document_index(
    datos_raw: dict[str, Any] | None,
    entries: dict[str, dict[str, str | None]],
) -> tuple[dict[str, Any], bool]:
    """Non-destructive DocumentId → RetrieveFile URL cache (Slice 4, task 4.4b).

    Persists every SECOP DocumentId seen from ANY source (Socrata archive doc
    rows, modification stubs, the manual scraper fallback) into the reserved
    ``datos_raw["_document_index"]`` key, so a captcha-free direct fetch of
    ``/Public/Archive/RetrieveFile/Index?DocumentId={id}`` can be tried before
    invoking the fragile/quota-limited scraper (live-verified 2026-07-11: this
    endpoint never triggers a captcha once a document is known).

    ``entries`` maps a DocumentId to a partial dict with any of ``url``,
    ``source`` (``"socrata_archive"`` | ``"modificacion"`` | ``"scraper"``),
    ``sha256``. Mirrors the ``_upsert_nuevos_datasets_raw`` non-destructive
    philosophy: a field is only overwritten when the new entry provides a
    non-empty value — a document seen once is never "un-seen", and a known
    URL/hash is never downgraded back to unknown.
    """
    raw = dict(datos_raw or {})
    index = {k: dict(v) for k, v in (raw.get("_document_index") or {}).items()}
    changed = False
    for doc_id, fields in entries.items():
        if not doc_id:
            continue
        current = index.get(doc_id, {})
        for field_name, value in fields.items():
            if value and current.get(field_name) != value:
                current[field_name] = value
                changed = True
        index[doc_id] = current
    if changed:
        raw["_document_index"] = index
    return raw, changed


def _extract_url_descarga(row: dict[str, Any]) -> str | None:
    """Normalize Socrata's `url_descarga_documento` field (dict-with-`url` or
    plain string) into a single optional URL string."""
    val = row.get("url_descarga_documento")
    if isinstance(val, dict):
        return val.get("url")
    return val


async def _query_docs_datasets(where_clause: str, failed_out: list[str] | None = None) -> list[dict[str, Any]]:
    """Fan-out: query all archive document datasets in parallel.

    Each row is annotated with ``_secop_dataset`` so callers can derive ``tipo_origen``.
    Errors from individual datasets are logged and skipped (partial results are fine).
    When ``failed_out`` is provided, the ids of datasets that errored are appended to
    it so callers can tell "the contract has few docs" from "Socrata throttled us".
    """
    raw_results = await asyncio.gather(
        *[_query_socrata(ds, where_clause, limit=1000) for ds in _ALL_DOCS_DATASETS],
        return_exceptions=True,
    )
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for ds_id, result in zip(_ALL_DOCS_DATASETS, raw_results, strict=False):
        if isinstance(result, Exception):
            log.warning("secop_docs_dataset_error", dataset=ds_id, error=str(result))
            if failed_out is not None:
                failed_out.append(ds_id)
            continue
        for raw in result:
            # Dedup by SECOP document id across datasets: the archive datasets can
            # overlap, so the same document may come back from more than one. Keep the
            # first occurrence; rows without an id_documento can't be deduped, keep them.
            doc_id = str(raw.get("id_documento") or "").strip()
            if doc_id:
                if doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
            annotated = dict(raw)
            annotated["_secop_dataset"] = ds_id
            rows.append(annotated)
    return rows


async def _query_modificaciones_docs(
    id_contratos_secop: list[str],
    referencia_contrato: str | None,
) -> list[dict[str, Any]]:
    """Query u8cx-r425 for modification PDFs and return synthetic document rows.

    Only rows that have a valid ``archivo_version_anterior`` URL and a unique
    ``identificador`` are included.  The returned dicts are shaped to match the
    fields expected by ``_upsert_documento``.
    """
    if not id_contratos_secop:
        return []

    safe_ids = " OR ".join(f"id_contrato = '{idc.replace(chr(39), chr(39) + chr(39))}'" for idc in id_contratos_secop)
    try:
        rows = await _query_socrata(_DS_MODIFICACIONES, where_clause=safe_ids, limit=200)
    except ExternalServiceError as exc:
        log.warning("secop_modificaciones_error", error=str(exc))
        return []

    synthetic: list[dict[str, Any]] = []
    seen_mods: set[str] = set()
    for row in rows:
        # Use identificador_modificacion (modification-level) as dedup key so each unique
        # modification produces one doc.  Fall back to identificador (contract-level) when
        # identificador_modificacion is absent (older dataset rows).
        dedup_key = str(row.get("identificador_modificacion") or row.get("identificador") or "").strip()
        if not dedup_key or dedup_key in seen_mods:
            continue
        seen_mods.add(dedup_key)

        raw_av = str(row.get("archivo_version_anterior") or "").strip()
        # Accept HTTP URLs directly; Marketplace IDs ("MarketplaceCO1...") are stored
        # as-is but url_descarga is set to None (no direct download link available).
        url_descarga: str | None = raw_av if raw_av.startswith("http") else None

        nombre = f"Modificación: {row.get('proposito_modificacion') or row.get('descripcion') or 'sin descripción'}"
        synth: dict[str, Any] = {
            **row,
            # Override with synthetic fields compatible with _upsert_documento
            "id_documento": f"MOD-{dedup_key}",
            "n_mero_de_contrato": referencia_contrato,
            "proceso": None,
            "nombre_archivo": nombre,
            "extensi_n": "pdf",
            "descripci_n": row.get("descripcion") or row.get("proposito_modificacion"),
            "fecha_carga": row.get("fecha_de_aprobacion"),
            "entidad": None,
            "nit_entidad": None,
            "url_descarga_documento": url_descarga,
            "tamanno_archivo": None,
            "_secop_dataset": _DS_MODIFICACIONES,
            # Preserve the raw identifier for frontend reference
            "_archivo_identificador": raw_av if not raw_av.startswith("http") else None,
        }
        synthetic.append(synth)
    return synthetic


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
        "url_descarga": _extract_url_descarga(row),
        "datos_raw": row,
    }

    if obj is None:
        obj = SecopDocumento(**fields)
        db.add(obj)
    else:
        for k, v in fields.items():
            setattr(obj, k, v)

    # Classify after setting fields; respects categoria_override on existing rows
    from app.services.document_classifier import aplicar_clasificacion

    aplicar_clasificacion(obj)

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
    datasets_con_error_out: list[str] | None = None,
) -> list[SecopDocumentoResponse]:
    """Fetch ALL documents for a contract by both numero_contrato and proceso_de_compra.

    Supports contracts with multiple SECOP rows (original + addenda / modifications).
    SECOP documents are linked to contracts via two different keys:
    - n_mero_de_contrato — direct contract ref (often the internal reference)
    - proceso (CO1.BDOS.xxx) — process docs, often contain the contract minute / obligations
    Both sets are merged and deduplicated to return the full document set.

    When the contract is not yet cached in the local DB, it is fetched from the
    SECOP II contratos dataset (jbjy-vk9h) so that proceso_de_compra is populated
    before running the document queries. Without this step, the document fan-out
    would have no proceso keys and would return zero results.

    ``datasets_con_error_out`` is an optional out-parameter (default ``None``,
    fully backward compatible): when a caller passes a list, any archive dataset
    that errors during this call's refresh is appended to it — used by
    `obtener_documentos_con_cobertura` (task 2.10b) to build its `cobertura`
    block without duplicating this function's fetch logic.
    """
    # Resolve ALL SecopContrato rows for this numero_contrato.
    # A contract can have multiple rows (original + adiciones/modificaciones),
    # each potentially with a different proceso_de_compra that hosts its own documents.
    # Using scalars().all() instead of scalar_one_or_none() prevents MultipleResultsFound.
    contrato_result = await db.execute(
        select(SecopContrato).where(
            or_(
                SecopContrato.numero_contrato == numero_contrato,
                SecopContrato.referencia_del_contrato == numero_contrato,
                SecopContrato.proceso_de_compra == numero_contrato,
            )
        )
    )
    secop_contratos = contrato_result.scalars().all()

    # ── Pre-fetch from Socrata when contract not yet cached ───────────────────
    # Without this, procesos_de_compra stays empty and document queries return nothing.
    # Real fields in jbjy-vk9h: referencia_del_contrato, proceso_de_compra, id_contrato.
    # NOTE: 'numero_contrato' does NOT exist in that dataset — only referencia_del_contrato.
    if not secop_contratos:
        safe_num = numero_contrato.replace("'", "''")
        try:
            api_rows = await _query_socrata(
                _DS_CONTRATOS,
                (
                    f"referencia_del_contrato = '{safe_num}'"
                    f" OR proceso_de_compra = '{safe_num}'"
                    f" OR id_contrato = '{safe_num}'"
                ),
                limit=10,
            )
        except Exception:
            api_rows = []
        for row in api_rows:
            await _upsert_contrato(db, row)
        if api_rows:
            await db.commit()
            contrato_result2 = await db.execute(
                select(SecopContrato).where(
                    or_(
                        SecopContrato.numero_contrato == numero_contrato,
                        SecopContrato.referencia_del_contrato == numero_contrato,
                        SecopContrato.proceso_de_compra == numero_contrato,
                    )
                )
            )
            secop_contratos = contrato_result2.scalars().all()
            log.info(
                "secop_contract_prefetched",
                numero_contrato=numero_contrato,
                found=len(secop_contratos),
            )

    # Use the first match as the primary FK anchor
    secop_contrato = secop_contratos[0] if secop_contratos else None
    contrato_id = secop_contrato.id if secop_contrato else None

    # Collect ALL unique proceso_de_compra values (one per addendum)
    procesos_de_compra: list[str] = list(
        dict.fromkeys(c.proceso_de_compra for c in secop_contratos if c.proceso_de_compra)
    )

    # Collect all linking keys to use as n_mero_de_contrato values in doc queries.
    # Some entities store referencia_del_contrato, others id_contrato (CO1.PCCNTR.xxx).
    referencias: list[str] = list(
        dict.fromkeys(
            ref
            for c in secop_contratos
            for ref in [c.referencia_del_contrato, c.numero_contrato, c.id_contrato_secop]
            if ref and ref != numero_contrato
        )
    )

    # Resolve SecopProceso FK for the primary proceso
    proceso_id: uuid.UUID | None = None
    if procesos_de_compra:
        proceso_result = await db.execute(
            select(SecopProceso).where(SecopProceso.id_proceso_secop == procesos_de_compra[0])
        )
        secop_proceso = proceso_result.scalar_one_or_none()
        proceso_id = secop_proceso.id if secop_proceso else None

    # Build cache condition: match any referencia OR any proceso
    all_num_keys = [numero_contrato] + referencias
    cache_conditions: list[Any] = [SecopDocumento.numero_contrato.in_(all_num_keys)]
    if procesos_de_compra:
        cache_conditions.append(SecopDocumento.proceso.in_(procesos_de_compra))
    cached_result = await db.execute(select(SecopDocumento).where(or_(*cache_conditions)))
    cached = cached_result.scalars().all()

    needs_refresh = refresh or not cached or (cached and not _is_fresh(cached[0].updated_at, _CACHE_TTL_DOCS))

    if needs_refresh:
        # Non-destructive refresh: upsert docs from SECOP without deleting cached ones.
        # This preserves references held by DocumentoCuentaCobro (ondelete=SET NULL) and
        # avoids data loss when SECOP returns 0 results due to a transient error.
        seen: set[str] = set()

        # ── Combined OR query — single fan-out to 4 datasets ─────────────────
        # Build one WHERE clause covering all contract refs and all proceso IDs.
        # Datasets index primarily by `proceso` (CO1.BDOS.xxx); `n_mero_de_contrato`
        # is also queried for completeness but is typically empty in practice.
        where_parts: list[str] = []
        for ref in all_num_keys:
            safe = ref.replace("'", "''")
            where_parts.append(f"n_mero_de_contrato = '{safe}'")
        for proc in procesos_de_compra:
            safe = proc.replace("'", "''")
            where_parts.append(f"proceso = '{safe}'")

        # Modifications dataset
        id_contratos_secop = [c.id_contrato_secop for c in secop_contratos if c.id_contrato_secop]
        referencia_contrato = (secop_contrato.referencia_del_contrato if secop_contrato else None) or numero_contrato
        mod_coro = _query_modificaciones_docs(id_contratos_secop, referencia_contrato)

        if where_parts:
            combined_where = " OR ".join(where_parts)
            docs_coro = _query_docs_datasets(combined_where, failed_out=datasets_con_error_out)
            gather_results = await asyncio.gather(docs_coro, mod_coro, return_exceptions=True)
            docs_rows: list[dict[str, Any]] = [] if isinstance(gather_results[0], Exception) else gather_results[0]
            mod_rows: list[dict[str, Any]] = [] if isinstance(gather_results[1], Exception) else gather_results[1]
            if isinstance(gather_results[0], Exception):
                log.warning("secop_docs_query_error", error=str(gather_results[0]))
            if isinstance(gather_results[1], Exception):
                log.warning("secop_mods_query_error", error=str(gather_results[1]))
        else:
            mod_result = await mod_coro
            docs_rows = []
            mod_rows = mod_result if not isinstance(mod_result, Exception) else []

        # Gap detection: no SECOP document dataset covers 2024 as a whole. A live
        # probe (2026-07-11, tasks.md Slice 1 task 1.2) confirmed dmgg-8hin only
        # holds a single 2024-12-31 bulk-load batch (not full-year 2024 coverage)
        # and 3skv-9na7 returns zero 2024 rows — so the gap is real for most of
        # 2024, even though it is already included in _ALL_DOCS_DATASETS above.
        # Log a warning when the contract's start date falls in 2024 and no documents
        # were found — avoids silently returning empty results without explanation.
        if not docs_rows and not mod_rows and secop_contrato is not None:
            contrato_year = secop_contrato.fecha_inicio.year if secop_contrato.fecha_inicio else None
            if contrato_year == 2024:
                log.warning(
                    "secop_docs_gap_2024",
                    numero_contrato=numero_contrato,
                    note="gap_2024_partial_dataset",
                    detail=(
                        "No SECOP document dataset fully covers 2024 (dmgg-8hin only holds a "
                        "2024-12-31 bulk-load batch). Documents for contracts starting in 2024 "
                        "may not be available via the public API."
                    ),
                )

        # Task 4.4b: collect every DocumentId + RetrieveFile URL seen this
        # refresh (regardless of `seen`-dedup for the upsert itself) into the
        # reserved `_document_index` cache on the parent SecopContrato.
        index_entries: dict[str, dict[str, str | None]] = {}
        for row in docs_rows:
            id_doc = str(row.get("id_documento") or "").strip()
            if id_doc and id_doc not in seen:
                seen.add(id_doc)
                row["_tipo_origen"] = "proceso" if row.get("proceso") else "contrato"
                await _upsert_documento(db, row, secop_contrato_id=contrato_id, secop_proceso_id=proceso_id)
            if id_doc:
                index_entries[id_doc] = {"url": _extract_url_descarga(row), "source": "socrata_archive"}
        for row in mod_rows:
            id_doc = str(row.get("id_documento") or "").strip()
            if id_doc and id_doc not in seen:
                seen.add(id_doc)
                row["_tipo_origen"] = "modificacion"
                await _upsert_documento(db, row, secop_contrato_id=contrato_id, secop_proceso_id=proceso_id)
            if id_doc:
                index_entries[id_doc] = {"url": _extract_url_descarga(row), "source": "modificacion"}

        if secop_contrato is not None and index_entries:
            new_raw, index_changed = upsert_document_index(secop_contrato.datos_raw, index_entries)
            if index_changed:
                secop_contrato.datos_raw = new_raw

        if seen:
            await db.commit()
        # Always re-read from DB after a refresh attempt (even if SECOP returned nothing new)
        cached_result = await db.execute(select(SecopDocumento).where(or_(*cache_conditions)))
        cached = cached_result.scalars().all()

    responses: list[SecopDocumentoResponse] = []
    for d in cached:
        r = SecopDocumentoResponse.model_validate(d)
        r.tipo_origen = _compute_tipo_origen(d)
        r.archivo_identificador = (d.datos_raw or {}).get("_archivo_identificador")
        responses.append(r)
    return _inject_url_proceso(responses, secop_contrato)


def _inject_url_proceso(
    docs: list[SecopDocumentoResponse],
    secop_contrato: SecopContrato | None,
) -> list[SecopDocumentoResponse]:
    """Inject url_proceso from SecopContrato.datos_raw['urlproceso'] into each document."""
    url_val = _extract_urlproceso(secop_contrato)
    if not url_val:
        return docs
    for doc in docs:
        doc.url_proceso = url_val
    return docs


def _compute_tipo_origen(doc: SecopDocumento) -> str:
    """Derive tipo_origen at runtime from datos_raw metadata (no DB column needed)."""
    raw = doc.datos_raw or {}
    dataset = raw.get("_secop_dataset", "")
    if dataset == _DS_MODIFICACIONES:
        return "modificacion"
    # _tipo_origen is tagged on each row before upsert to record how it was found:
    # "contrato" → found via n_mero_de_contrato (referencia_del_contrato is the key)
    # "proceso"  → found via proceso_de_compra
    tipo = raw.get("_tipo_origen")
    if tipo in ("contrato", "proceso", "modificacion"):
        return tipo
    # Fallback for rows cached before this field was introduced
    if doc.numero_contrato:
        return "contrato"
    return "proceso"


def _extract_urlproceso(secop_contrato: SecopContrato | None) -> str | None:
    """Extract the SECOP II platform URL from `SecopContrato.datos_raw['urlproceso']`."""
    if not secop_contrato:
        return None
    raw = secop_contrato.datos_raw or {}
    url_val = raw.get("urlproceso")
    if isinstance(url_val, dict):
        url_val = url_val.get("url")
    if not url_val or not str(url_val).startswith("http"):
        return None
    return str(url_val)


def _compute_cobertura(
    secop_contrato: SecopContrato | None,
    datasets_con_error: list[str],
    documentos: list[SecopDocumentoResponse],
) -> CoberturaDocumentosInfo:
    """Build the `cobertura` block (task 2.10b): explains partial document
    coverage instead of leaving it looking like a bug. Gap flag is True when
    either the contract falls in the known-partial 2024 window (D4) or any
    archive dataset errored during this fetch."""
    razones: list[str] = []
    gap = False

    if secop_contrato is not None and secop_contrato.fecha_inicio and secop_contrato.fecha_inicio.year == 2024:
        gap = True
        razones.append(
            "Este contrato inicia en 2024, año en el que los datasets públicos de SECOP tienen "
            "cobertura parcial (ver 'secop_docs_gap_2024'): documentos del inicio del contrato "
            "pueden no estar disponibles vía la API de datos abiertos."
        )

    if datasets_con_error:
        gap = True
        razones.append(
            "Los siguientes datasets de SECOP fallaron durante la consulta y el resultado puede "
            f"estar incompleto: {', '.join(sorted(set(datasets_con_error)))}."
        )

    if not gap:
        razones.append(
            "La cobertura de documentos de datos abiertos para este contrato es la esperada. "
            "Documentos adicionales, si existen, solo están disponibles en la plataforma SECOP II."
        )

    return CoberturaDocumentosInfo(
        gap=gap,
        razon=" ".join(razones),
        url_proceso=_extract_urlproceso(secop_contrato),
    )


async def obtener_documentos_con_cobertura(
    db: AsyncSession,
    numero_contrato: str,
    refresh: bool = False,
) -> SecopDocumentosConCoberturaResponse:
    """Documents for a contract plus a structured `cobertura` explanation (task
    2.10b) — used by `GET /secop/documentos/{numero_contrato}` so a legitimately
    small document count (e.g. a 2024 contract, or a Socrata outage) doesn't look
    like a bug to the caller. Delegates entirely to `buscar_documentos_contrato`
    (same cache/refresh/dedup behavior), only adding the coverage explanation.
    """
    datasets_con_error: list[str] = []
    documentos = await buscar_documentos_contrato(
        db, numero_contrato, refresh=refresh, datasets_con_error_out=datasets_con_error
    )

    contrato_result = await db.execute(
        select(SecopContrato).where(
            or_(
                SecopContrato.numero_contrato == numero_contrato,
                SecopContrato.referencia_del_contrato == numero_contrato,
                SecopContrato.proceso_de_compra == numero_contrato,
            )
        )
    )
    secop_contrato = contrato_result.scalars().first()

    return SecopDocumentosConCoberturaResponse(
        documentos=documentos,
        cobertura=_compute_cobertura(secop_contrato, datasets_con_error, documentos),
    )


async def listar_archivos_comprimido(
    db: AsyncSession,
    doc_id: uuid.UUID,
) -> ArchivoComprimidoResponse:
    """Descarga un documento comprimido (.zip) y lista sus archivos internos.

    Para archivos .rar devuelve un error informativo (requiere dependencia externa).
    Para otros formatos devuelve una lista vacía con un mensaje.
    """
    result = await db.execute(select(SecopDocumento).where(SecopDocumento.id == doc_id))
    doc = result.scalar_one_or_none()

    if doc is None:
        return ArchivoComprimidoResponse(
            doc_id=str(doc_id),
            nombre_archivo=None,
            extension=None,
            archivos=[],
            error="Documento no encontrado",
        )

    ext = (doc.extension or "").lower().strip(".")
    if not doc.url_descarga:
        return ArchivoComprimidoResponse(
            doc_id=str(doc_id),
            nombre_archivo=doc.nombre_archivo,
            extension=doc.extension,
            archivos=[],
            error="El documento no tiene URL de descarga",
        )

    if ext == "rar":
        return ArchivoComprimidoResponse(
            doc_id=str(doc_id),
            nombre_archivo=doc.nombre_archivo,
            extension=doc.extension,
            archivos=[],
            error="Formato RAR: no es posible listar el contenido sin herramientas adicionales",
        )

    if ext != "zip":
        return ArchivoComprimidoResponse(
            doc_id=str(doc_id),
            nombre_archivo=doc.nombre_archivo,
            extension=doc.extension,
            archivos=[],
            error=f"El archivo no es un comprimido soportado (extensión: {doc.extension or 'desconocida'})",
        )

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(doc.url_descarga)
            response.raise_for_status()
        content = response.content
    except httpx.HTTPError as exc:
        return ArchivoComprimidoResponse(
            doc_id=str(doc_id),
            nombre_archivo=doc.nombre_archivo,
            extension=doc.extension,
            archivos=[],
            error=f"No se pudo descargar el archivo: {exc}",
        )

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            archivos = [
                ArchivoInternoItem(
                    nombre=info.filename,
                    tamanio_bytes=info.file_size if info.file_size > 0 else None,
                    es_directorio=info.filename.endswith("/"),
                )
                for info in zf.infolist()
            ]
    except zipfile.BadZipFile:
        return ArchivoComprimidoResponse(
            doc_id=str(doc_id),
            nombre_archivo=doc.nombre_archivo,
            extension=doc.extension,
            archivos=[],
            error="El archivo no es un ZIP válido",
        )

    return ArchivoComprimidoResponse(
        doc_id=str(doc_id),
        nombre_archivo=doc.nombre_archivo,
        extension=doc.extension,
        archivos=archivos,
    )


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

    detalles = list(await asyncio.gather(*[_enriquecer(c) for c in contratos]))

    return SecopConsultaCompletaResponse(
        cedula=cedula,
        total_contratos=len(contratos),
        contratos=detalles,
    )


# ---------------------------------------------------------------------------
# Importar contratos SECOP → tabla contratos del usuario
# ---------------------------------------------------------------------------


def _meses_calendario(fecha_inicio: date, fecha_fin: date) -> int:
    """Count the months of service a contract spans, for monthly-value proration.

    Uses the month difference and adds one extra month only when the end day is
    STRICTLY past the start day, i.e. the final partial month is actually served:
      - Jan 1 → Feb 1  → 1  (one month of service, not two calendar months)
      - Jan 1 → Jan 1 (next year) → 12
      - Jan 19 → Jun 30 → 6  (30 > 19 → the last partial month counts)
      - same day → 0 → clamped to 1
    A `>=` here would over-count anniversary/first-of-month endpoints by one,
    understating the monthly value (e.g. a 1-month contract billed as ½).
    """
    meses = (fecha_fin.year - fecha_inicio.year) * 12 + (fecha_fin.month - fecha_inicio.month)
    if fecha_fin.day > fecha_inicio.day:
        meses += 1
    return max(1, meses)


def _calcular_valor_mensual(valor_total: Decimal, fecha_inicio: date, fecha_fin: date) -> Decimal:
    meses = _meses_calendario(fecha_inicio, fecha_fin)
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
        str(row.get("objeto_del_contrato") or "").strip() or str(row.get("descripcion_del_proceso") or "").strip()
    )[:2000]
    if len(objeto) < 10:
        return None

    # --- valor_total (base + adición acumulada) ---
    try:
        valor_base = Decimal(str(row.get("valor_del_contrato") or 0)).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None
    if valor_base <= 0:
        return None

    try:
        valor_adicion = Decimal(str(row.get("valor_adicion") or 0)).quantize(Decimal("0.01"))
        if valor_adicion < 0:
            valor_adicion = Decimal("0.00")
    except InvalidOperation:
        valor_adicion = Decimal("0.00")

    valor_total = valor_base + valor_adicion

    # --- fechas ---
    fecha_inicio = _parse_date(row.get("fecha_de_inicio_del_contrato"))
    fecha_fin = _parse_date(row.get("fecha_de_fin_del_contrato"))
    if not fecha_inicio or not fecha_fin or fecha_fin <= fecha_inicio:
        return None

    # --- valor_mensual: valor_total / meses calendario ---
    valor_mensual = _calcular_valor_mensual(valor_total, fecha_inicio, fecha_fin)

    # --- campos opcionales ---
    supervisor = str(row.get("nombre_supervisor") or "").strip() or None
    if supervisor:
        supervisor = supervisor[:255]

    entidad = str(row.get("nombre_entidad") or "").strip() or None
    if entidad:
        entidad = entidad[:255]

    # Best-effort, deterministic first pass: SECOP's "objeto" is usually a short
    # paragraph with no enumerated obligations section, so this normally yields
    # nothing — the LLM fallback in importar_contratos_secop picks up from there.
    obligaciones = [
        ObligacionCreate(descripcion=o.descripcion, tipo=o.tipo, orden=o.orden, etiqueta=o.etiqueta)
        for o in extract_obligaciones_verbatim(objeto)
    ]

    return ContratoCreate(
        numero_contrato=numero,
        objeto=objeto,
        valor_total=valor_total,
        valor_adicion=valor_adicion if valor_adicion > 0 else None,
        valor_mensual=valor_mensual,
        fecha_inicio=fecha_inicio,
        fecha_fin=fecha_fin,
        supervisor_nombre=supervisor,
        entidad=entidad,
        dependencia=None,
        obligaciones=obligaciones,
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

    importados: list[SecopContratoImportado] = []
    omitidos_duplicados = 0
    omitidos_invalidos = 0
    actualizados = 0
    llm_obligaciones_intentos = 0

    from sqlalchemy.orm import selectinload

    def _to_importado(resp: ContratoResponse) -> SecopContratoImportado:
        return SecopContratoImportado(**resp.model_dump(), requiere_obligaciones=not resp.obligaciones)

    for row in rows:
        data = _mapear_a_contrato_create(row)
        if data is None:
            omitidos_invalidos += 1
            continue

        es_duplicado = data.numero_contrato in existing_numeros

        if not confirmar:
            # Preview mode: build response without persisting. Obligaciones are
            # never populated here (no persisted ids to attach them to), so the
            # signal always reflects that follow-up is needed once confirmed.
            importados.append(
                _to_importado(
                    ContratoResponse(
                        id=uuid.uuid4(),
                        usuario_id=usuario_id,
                        numero_contrato=data.numero_contrato,
                        objeto=data.objeto,
                        valor_total=data.valor_total,
                        valor_adicion=data.valor_adicion,
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
                    )
                )
            )
            existing_numeros.add(data.numero_contrato)
            continue

        if es_duplicado:
            # UPSERT: update valor fields on existing record
            upd_result = await db.execute(
                select(Contrato).where(
                    Contrato.usuario_id == usuario_id,
                    Contrato.numero_contrato == data.numero_contrato,
                    Contrato.deleted_at.is_(None),
                )
            )
            existing_contrato = upd_result.scalar_one_or_none()
            if existing_contrato is not None:
                existing_contrato.valor_total = float(data.valor_total)
                existing_contrato.valor_adicion = float(data.valor_adicion) if data.valor_adicion else None
                existing_contrato.valor_mensual = float(data.valor_mensual)
                existing_contrato.fecha_inicio = data.fecha_inicio
                existing_contrato.fecha_fin = data.fecha_fin
                if data.supervisor_nombre:
                    existing_contrato.supervisor_nombre = data.supervisor_nombre
                if data.entidad:
                    existing_contrato.entidad = data.entidad
                await db.flush()

                # C.3 reimport guard: a contract first imported with zero
                # obligaciones must not stay stuck forever — if this reimport's
                # objeto now yields obligaciones (verbatim), add them and flip
                # the flag. NEVER clobber an already-True flag/existing obligaciones.
                db.expire(existing_contrato, ["obligaciones"])
                await db.refresh(existing_contrato, ["obligaciones"])
                if not existing_contrato.obligaciones and data.obligaciones:
                    for ob in data.obligaciones:
                        db.add(
                            Obligacion(
                                contrato_id=existing_contrato.id,
                                descripcion=ob.descripcion,
                                tipo=TipoObligacion(ob.tipo),
                                orden=ob.orden,
                                etiqueta=ob.etiqueta,
                            )
                        )
                    existing_contrato.obligaciones_extraidas = True
                    await db.flush()
                    # The `obligaciones` collection just added above was already
                    # marked "loaded" (empty) by the refresh() a few lines up —
                    # without expiring it again, the `selectinload` in the final
                    # re-select below skips reloading it (already-loaded relationships
                    # aren't re-fetched) and would return the stale empty list.
                    db.expire(existing_contrato, ["obligaciones"])

                result = await db.execute(
                    select(Contrato)
                    .options(selectinload(Contrato.obligaciones))
                    .where(Contrato.id == existing_contrato.id)
                )
                importados.append(_to_importado(ContratoResponse.model_validate(result.scalar_one())))
                actualizados += 1
            else:
                omitidos_duplicados += 1
            continue

        contrato = Contrato(
            usuario_id=usuario_id,
            numero_contrato=data.numero_contrato,
            objeto=data.objeto,
            valor_total=data.valor_total,
            valor_adicion=data.valor_adicion,
            valor_mensual=data.valor_mensual,
            fecha_inicio=data.fecha_inicio,
            fecha_fin=data.fecha_fin,
            supervisor_nombre=data.supervisor_nombre,
            entidad=data.entidad,
            dependencia=data.dependencia,
            documento_proveedor=documento_proveedor,
            # C.3: deterministic signal at construction time; reconciled below once
            # the LLM-fallback branch (if any) resolves — a contract that starts
            # with zero verbatim obligaciones can still end up True.
            obligaciones_extraidas=bool(data.obligaciones),
        )
        db.add(contrato)
        await db.flush()

        if data.obligaciones:
            for ob in data.obligaciones:
                db.add(
                    Obligacion(
                        contrato_id=contrato.id,
                        descripcion=ob.descripcion,
                        tipo=TipoObligacion(ob.tipo),
                        orden=ob.orden,
                        etiqueta=ob.etiqueta,
                    )
                )
            await db.flush()
        elif llm_obligaciones_intentos >= SECOP_OBLIGACIONES_LLM_MAX_CONTRATOS:
            # Per-run cap reached — the LLM fallback (retries x timeout x
            # fallback chain) is too slow to run for every contract in a bulk
            # import. Remaining contracts simply keep requiere_obligaciones=true
            # for manual follow-up.
            log.info(
                "secop_obligaciones_llm_skipped_cap",
                contrato_id=str(contrato.id),
                numero_contrato=data.numero_contrato,
                max_contratos=SECOP_OBLIGACIONES_LLM_MAX_CONTRATOS,
            )
        else:
            # Best-effort LLM fallback — must never fail or hang the import.
            llm_obligaciones_intentos += 1
            try:
                from app.services.document_service import extraer_obligaciones_texto

                await asyncio.wait_for(
                    extraer_obligaciones_texto(data.objeto, contrato.id, db),
                    timeout=SECOP_OBLIGACIONES_LLM_TIMEOUT_S,
                )
            except TimeoutError:
                log.warning(
                    "secop_obligaciones_extraction_timeout",
                    contrato_id=str(contrato.id),
                    numero_contrato=data.numero_contrato,
                    timeout_s=SECOP_OBLIGACIONES_LLM_TIMEOUT_S,
                )
            except Exception as exc:
                log.warning(
                    "secop_obligaciones_extraction_failed",
                    contrato_id=str(contrato.id),
                    numero_contrato=data.numero_contrato,
                    error=str(exc),
                )

        existing_numeros.add(data.numero_contrato)

        result = await db.execute(
            select(Contrato).options(selectinload(Contrato.obligaciones)).where(Contrato.id == contrato.id)
        )
        contrato_final = result.scalar_one()
        # C.3 under-report guard (7a.4): reconcile AFTER the LLM-fallback branch
        # resolves — obligaciones inserted only by the fallback (not present at
        # construction time) must still flip the flag to True. Reusing this
        # reload's `selectinload(Contrato.obligaciones)` avoids an extra round-trip.
        contrato_final.obligaciones_extraidas = bool(contrato_final.obligaciones)
        importados.append(_to_importado(ContratoResponse.model_validate(contrato_final)))

    if confirmar:
        await db.commit()
    log.info(
        "secop_importar_done",
        documento_proveedor=documento_proveedor,
        importados=len(importados) - actualizados,
        actualizados=actualizados,
        omitidos_duplicados=omitidos_duplicados,
        omitidos_invalidos=omitidos_invalidos,
    )

    return SecopImportResult(
        documento_proveedor=documento_proveedor,
        encontrados_en_secop=len(rows),
        importados=len(importados) - actualizados,
        actualizados=actualizados,
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
    contratos_result = await db.execute(select(SecopContrato).where(SecopContrato.cedula_contratista == cedula))
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
    # Datasets that errored (e.g. Socrata throttling) across all fan-out calls, so
    # the caller can distinguish a genuinely doc-poor contract from a partial fetch.
    failed_datasets: list[str] = []

    async def _fetch_docs_for_contrato(contrato: SecopContrato) -> tuple[list[dict[str, Any]], SecopContrato]:
        if not contrato.numero_contrato:
            return [], contrato
        safe_num = contrato.numero_contrato.replace("'", "''")
        # Fan out across ALL archive datasets (2018→today), not just the 2025 one,
        # so contracts with pre-2025 documents are not silently under-reported.
        rows = await _query_docs_datasets(f"n_mero_de_contrato = '{safe_num}'", failed_out=failed_datasets)
        return rows, contrato

    async def _fetch_docs_for_proceso(proceso: SecopProceso) -> tuple[list[dict[str, Any]], SecopProceso]:
        safe_proc = proceso.id_proceso_secop.replace("'", "''")
        rows = await _query_docs_datasets(f"proceso = '{safe_proc}'", failed_out=failed_datasets)
        return rows, proceso

    # Fetch documents for all contratos and procesos in parallel
    gather_results = await asyncio.gather(
        *[_fetch_docs_for_contrato(c) for c in contratos],
        *[_fetch_docs_for_proceso(p) for p in procesos],
        return_exceptions=True,
    )

    contrato_results = gather_results[: len(contratos)]
    proceso_results = gather_results[len(contratos) :]

    def _url_from_row(row: dict[str, Any]) -> str | None:
        raw = row.get("url_descarga_documento")
        if isinstance(raw, dict):
            return raw.get("url")
        return raw

    # Process contrato results
    for result in contrato_results:
        if isinstance(result, Exception):
            log.warning("secop_sync_contrato_failed", error=str(result))
            continue
        rows, contrato = result
        for row in rows:
            id_doc = str(row.get("id_documento") or "").strip()
            if not id_doc:
                continue
            if id_doc in existing_ids:
                docs_omitidos += 1
                tmp = SecopDocumento(
                    id_documento_secop=id_doc,
                    numero_contrato=row.get("n_mero_de_contrato"),
                    proceso=row.get("proceso"),
                    secop_contrato_id=contrato.id,
                    nombre_archivo=row.get("nombre_archivo"),
                    extension=row.get("extensi_n"),
                    descripcion=row.get("descripci_n"),
                    url_descarga=_url_from_row(row),
                    datos_raw=row,
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
                all_docs.append(
                    SecopDocumento(
                        id_documento_secop=id_doc,
                        numero_contrato=row.get("n_mero_de_contrato"),
                        proceso=row.get("proceso"),
                        secop_contrato_id=contrato.id,
                        nombre_archivo=row.get("nombre_archivo"),
                        extension=row.get("extensi_n"),
                        descripcion=row.get("descripci_n"),
                        url_descarga=_url_from_row(row),
                    )
                )
                existing_ids.add(id_doc)
                docs_guardados += 1

    # Process proceso results
    for result in proceso_results:
        if isinstance(result, Exception):
            log.warning("secop_sync_proceso_failed", error=str(result))
            continue
        rows, proceso = result
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
                all_docs.append(
                    SecopDocumento(
                        id_documento_secop=id_doc,
                        numero_contrato=row.get("n_mero_de_contrato"),
                        proceso=row.get("proceso"),
                        secop_proceso_id=proceso.id,
                        nombre_archivo=row.get("nombre_archivo"),
                        extension=row.get("extensi_n"),
                        descripcion=row.get("descripci_n"),
                        url_descarga=_url_from_row(row),
                    )
                )
                existing_ids.add(id_doc)
                docs_guardados += 1

    # ── New datasets (Adiciones, Modificaciones a Procesos, Ubicaciones) ──────
    # Slice 2 (secop-dataset-ingestion, D1/D5): only wired when persisting, so
    # preview mode (confirmar=False) keeps its existing zero-extra-query behavior.
    # Non-destructive upsert: an empty/failed fetch for a given dataset leaves
    # whatever was previously cached on datos_raw untouched (see
    # `_upsert_nuevos_datasets_raw`).
    if confirmar and contratos:
        proceso_by_id = {p.id_proceso_secop: p for p in procesos}

        async def _fetch_nuevos_para_contrato(
            contrato: SecopContrato,
        ) -> tuple[SecopContrato, dict[str, list[dict[str, Any]]], list[str]]:
            proceso_obj = proceso_by_id.get(contrato.proceso_de_compra) if contrato.proceso_de_compra else None
            local_failed: list[str] = []
            nuevos = await _fetch_nuevos_datasets_contrato(
                id_contrato_secop=contrato.id_contrato_secop,
                proceso_de_compra=contrato.proceso_de_compra,
                id_del_portafolio=proceso_obj.id_proceso_secop if proceso_obj else None,
                referencia_del_contrato=contrato.referencia_del_contrato,
                failed_out=local_failed,
            )
            return contrato, nuevos, local_failed

        nuevos_results = await asyncio.gather(
            *[_fetch_nuevos_para_contrato(c) for c in contratos], return_exceptions=True
        )
        for nuevos_result in nuevos_results:
            if isinstance(nuevos_result, BaseException):
                log.warning("secop_nuevos_datasets_contrato_failed", error=str(nuevos_result))
                continue
            contrato, nuevos, local_failed = nuevos_result
            failed_datasets.extend(local_failed)

            raw, changed = _upsert_nuevos_datasets_raw(contrato.datos_raw, nuevos)
            # Error status reflects THIS run's outcome (overwritten, not merged) —
            # unlike the data rows above, a dataset that recovers should stop
            # showing as errored for `obtener_estado_datasets_secop`.
            if local_failed:
                raw["_dataset_errors"] = sorted(set(local_failed))
                changed = True
            elif "_dataset_errors" in raw:
                del raw["_dataset_errors"]
                changed = True
            if changed:
                contrato.datos_raw = raw

            modif_rows = nuevos.get("_modif_procesos")
            if modif_rows:
                proceso_obj = proceso_by_id.get(contrato.proceso_de_compra) if contrato.proceso_de_compra else None
                if proceso_obj is not None:
                    proceso_raw = dict(proceso_obj.datos_raw or {})
                    proceso_raw["_modif_procesos"] = modif_rows
                    proceso_obj.datos_raw = proceso_raw

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
        docs_response.append(
            SecopDocumentoResponse(
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
            )
        )

    return SecopSincronizarDocumentosResult(
        contratos_procesados=len(contratos),
        procesos_procesados=len(procesos),
        documentos_encontrados=len(all_docs),
        documentos_guardados=docs_guardados,
        documentos_omitidos_duplicados=docs_omitidos,
        confirmar=confirmar,
        documentos=docs_response,
        datasets_con_error=sorted(set(failed_datasets)),
    )


# ---------------------------------------------------------------------------
# Accessors for the 3 new datasets (secop-dataset-ingestion, Slice 2)
# ---------------------------------------------------------------------------


async def obtener_adiciones_contrato(db: AsyncSession, id_contrato: str) -> list[dict[str, Any]]:
    """Boundary accessor (design D2) — reads cached Adición rows from
    ``SecopContrato.datos_raw["_adiciones"]``. Never queries Socrata directly:
    this is the single ingestion owner; `billing-resilience-templates` (migration
    026) consumes this accessor instead of duplicating the fetch.

    Returns a list of ``{id_contrato_secop, numero_contrato, valor_adicion,
    fecha_efectiva, raw}`` dicts. ``valor_adicion`` is best-effort-parsed from
    cb9c-h8sn's free-text ``descripcion`` (no structured value field exists —
    see the ``_DS_ADICIONES`` deviation note) and may be ``None`` when no peso
    amount could be extracted; callers must handle that case.
    """
    result = await db.execute(select(SecopContrato).where(SecopContrato.id_contrato_secop == id_contrato))
    contrato = result.scalar_one_or_none()
    if contrato is None:
        return []

    adiciones_raw = (contrato.datos_raw or {}).get("_adiciones") or []
    return [
        {
            "id_contrato_secop": row.get("id_contrato") or id_contrato,
            "numero_contrato": contrato.numero_contrato,
            "valor_adicion": _extraer_valor_adicion_texto(row.get("descripcion")),
            "fecha_efectiva": _parse_date(row.get("fecharegistro")),
            "raw": row,
        }
        for row in adiciones_raw
    ]


async def obtener_estado_datasets_secop(db: AsyncSession, cedula: str) -> SecopEstadoDatasetsResponse:
    """Read-only diagnostic (task 2.9): surfaces per-dataset schema-verification
    status for the 3 new datasets plus any accumulated `datasets_con_error`,
    aggregated across every cached SecopContrato/SecopProceso for this cédula.

    Offline by design (D3 — "verify offline, wire constants"): reports the
    OUTCOME of the most recent `sincronizar_documentos_secop` run(s) recorded on
    cache rows, it does not itself query Socrata.
    """
    if not re.match(r"^\d{5,15}$", cedula):
        raise ValidationError("La cédula debe contener entre 5 y 15 dígitos")

    contratos_result = await db.execute(select(SecopContrato).where(SecopContrato.cedula_contratista == cedula))
    contratos = contratos_result.scalars().all()

    errores_acumulados: set[str] = set()
    tiene_adiciones = False
    tiene_ubicaciones = False
    for c in contratos:
        raw = c.datos_raw or {}
        errores_acumulados.update(raw.get("_dataset_errors") or [])
        if raw.get("_adiciones"):
            tiene_adiciones = True
        if raw.get("_ubicaciones"):
            tiene_ubicaciones = True

    proceso_ids = {c.proceso_de_compra for c in contratos if c.proceso_de_compra}
    tiene_modif_procesos = False
    if proceso_ids:
        procesos_result = await db.execute(
            select(SecopProceso).where(SecopProceso.id_proceso_secop.in_(list(proceso_ids)))
        )
        for p in procesos_result.scalars().all():
            raw = p.datos_raw or {}
            errores_acumulados.update(raw.get("_dataset_errors") or [])
            if raw.get("_modif_procesos"):
                tiene_modif_procesos = True

    def _estado(dataset_id: str, tiene_datos: bool) -> str:
        if dataset_id in errores_acumulados:
            return "error"
        return "ok" if tiene_datos else "no_verificado"

    datasets = [
        SecopEstadoDataset(
            dataset_id=_DS_ADICIONES,
            nombre="Adiciones",
            estado=_estado(_DS_ADICIONES, tiene_adiciones),
        ),
        SecopEstadoDataset(
            dataset_id=_DS_MODIF_PROCESOS,
            nombre="Modificaciones a Procesos",
            estado=_estado(_DS_MODIF_PROCESOS, tiene_modif_procesos),
        ),
        SecopEstadoDataset(
            dataset_id=_DS_UBICACIONES,
            nombre="Ubicaciones ejecución",
            estado=_estado(_DS_UBICACIONES, tiene_ubicaciones),
        ),
    ]

    return SecopEstadoDatasetsResponse(
        datasets_con_error=sorted(errores_acumulados),
        datasets=datasets,
    )


async def actualizar_categoria_documento(
    db: AsyncSession,
    doc_id: uuid.UUID,
    categoria: CategoriaDocumento,
    usuario_id: uuid.UUID | None = None,
) -> SecopDocumento:
    """Override the category of a SECOP document and mark it as manually set.

    When ``usuario_id`` is provided, verifies that the document belongs to a
    SecopContrato imported by that user (via cedula_contratista match on
    Contrato.documento_proveedor). Docs not linked to any user-owned contrato
    raise NotFoundError to prevent cross-user category overrides.
    """
    # Fetch the SECOP document, optionally scoped to a user-imported contrato.
    if usuario_id is not None:
        # Join SecopDocumento → SecopContrato and verify the cedula is used by this user.
        res = await db.execute(
            select(SecopDocumento)
            .join(SecopContrato, SecopDocumento.secop_contrato_id == SecopContrato.id, isouter=True)
            .join(
                Contrato,
                (Contrato.documento_proveedor == SecopContrato.cedula_contratista)
                & (Contrato.usuario_id == usuario_id)
                & (Contrato.deleted_at.is_(None)),
                isouter=True,
            )
            .where(
                SecopDocumento.id == doc_id,
                # Accept docs linked to a contrato the user owns, or unlinked docs
                # (secop_contrato_id IS NULL) which are global and always accessible.
                (SecopDocumento.secop_contrato_id.is_(None)) | (Contrato.id.is_not(None)),
            )
        )
    else:
        res = await db.execute(select(SecopDocumento).where(SecopDocumento.id == doc_id))

    doc = res.scalar_one_or_none()
    if doc is None:
        raise NotFoundError("SecopDocumento", str(doc_id))

    doc.categoria = categoria
    doc.categoria_confianza = None
    doc.categoria_override = True
    await db.flush()
    return doc


# ---------------------------------------------------------------------------
# Configuration diagnostics
# ---------------------------------------------------------------------------


async def verificar_configuracion_secop() -> SecopConfiguracionResponse:
    """Report whether the SECOP Socrata integration is configured, for
    support/diagnostics — mirrors the `/health/llm` diagnostic pattern.

    Read-only, no network call: surfaces the same `SECOP_APP_TOKEN` presence
    signal as the non-blocking startup warning in `Settings`
    (`_warn_if_secop_token_missing`), queryable on demand instead of only
    visible in startup logs.
    """
    token_configured = bool(settings.SECOP_APP_TOKEN)
    if token_configured:
        return SecopConfiguracionResponse(status="ok", token_configured=True)
    return SecopConfiguracionResponse(
        status="degraded",
        token_configured=False,
        warning=(
            "SECOP_APP_TOKEN is empty — Socrata will throttle unauthenticated requests; "
            "SECOP imports may be partial or fail."
        ),
    )
