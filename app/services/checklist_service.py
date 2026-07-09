"""Checklist service — required documents per cuenta de cobro.

Builds the per-cuenta state of every catalog requirement, detects matches in the
SECOP document cache via keyword scoring, and produces a logical evidence tree
(grouped by obligación) without materialising any folder in storage. Each cuenta
is independent: requirements are re-derived from the source documents on load,
never copied from a previous cuenta.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import structlog
from sqlalchemy import ColumnElement, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError
from app.core.text_match import keyword_score as _keyword_score
from app.core.text_match import similar as _similar
from app.core.text_match import solo_digitos as _solo_digitos
from app.models.actividad import Actividad
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    DocumentoRequisitoVinculo,
    EstadoRequisito,
)
from app.models.documento_fuente import DocumentoFuente
from app.models.obligacion import Obligacion
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.requisito_documento import RequisitoDocumento
from app.models.secop import SecopContrato, SecopDocumento
from app.services.document_classifier import CATEGORIA_A_REQUISITO, TIPO_A_REQUISITO

logger = structlog.get_logger("service.checklist")

# Reverse of CATEGORIA_A_REQUISITO: {requisito_codigo -> CategoriaDocumento}
_REQUISITO_A_CATEGORIA: dict[str, CategoriaDocumento] = {
    v: k for k, v in CATEGORIA_A_REQUISITO.items() if v is not None
}

# Auto-link threshold for SECOP detection (per session plan decision).
AUTO_LINK_THRESHOLD = Decimal("0.700")
TOP_N_CANDIDATES = 3

# Minimum fuzzy similarity between a contract identifier (numero_contrato) and a
# SECOP record's identifier to consider them the same contract. Tolerates case,
# spaces, accents and punctuation while still rejecting unrelated numbers.
_SECOP_ID_SIMIL_THRESHOLD = Decimal("0.850")
# Safety cap for the fuzzy fallback scan when no cedula/number anchor matches.
_SECOP_FUZZY_SCAN_LIMIT = 1000

# Two-tier document model. Contract-level requisitos are satisfied by a SINGLE
# shared document (cuenta_cobro_id IS NULL) that auto-fulfils the requisito in
# EVERY cuenta of the contract. Everything else is strictly per-cuenta.
_NIVEL_CONTRATO = frozenset({"CONTRATO", "RPC", "CEDULA", "RUT", "FICHA_TECNICA", "ACTA_INICIO"})


def es_nivel_contrato(requisito_codigo: str | None) -> bool:
    """Whether a standard requisito's document lives at contract level (shared across cuentas).

    Custom (per-cuenta) requisitos are never contract-level.
    """
    return requisito_codigo in _NIVEL_CONTRATO


# ── catalog access ─────────────────────────────────────────────────────────


# Code-side mirror of the alembic 011 seed. Used to bootstrap the catalog when
# the table is empty (e.g. SQLite tests that use Base.metadata.create_all and
# therefore never run the migration's bulk_insert).
_CATALOGO_SEED: list[dict] = [
    # Recurring (every cuenta)
    {
        "codigo": "CONTRATO",
        "etiqueta": "Contrato / minuta / clausulado",
        "descripcion": "Documento del contrato firmado.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "contrato",
        "keywords_deteccion": ["contrato", "minuta", "clausulado"],
        "orden": 10,
    },
    {
        "codigo": "RPC",
        "etiqueta": "Registro Presupuestal (RPC/RP)",
        "descripcion": "Registro Presupuestal del Compromiso.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "rpc",
        "keywords_deteccion": ["rpc", "registro presupuestal", "rp ", "compromiso presupuestal"],
        "orden": 20,
    },
    {
        "codigo": "SEGURIDAD_SOCIAL",
        "etiqueta": "Planilla de aportes a seguridad social",
        "descripcion": "Soporte de pago de la planilla del periodo.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "seguridad_social",
        "keywords_deteccion": ["seguridad social", "planilla", "pila"],
        "orden": 30,
    },
    {
        "codigo": "COMPROBANTE_PAGO_SS",
        "etiqueta": "Comprobante de pago seguridad social (CUS)",
        "descripcion": "Comprobante de la transacción de pago / CUS.",
        "obligatorio": False,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "comprobante_pago_ss",
        "keywords_deteccion": ["comprobante pago", "cus", "comprobante seguridad social"],
        "orden": 40,
    },
    {
        "codigo": "INFORME_ACTIVIDADES",
        "etiqueta": "Informe de actividades",
        "descripcion": "Informe del contratista (primera persona).",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": True,
        "tipo_documento_fuente": "informe_actividades",
        "keywords_deteccion": ["informe de actividades", "informe actividades"],
        "orden": 50,
    },
    {
        "codigo": "INFORME_SUPERVISION",
        "etiqueta": "Informe de supervisión",
        "descripcion": "Informe del supervisor (tercera persona).",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": True,
        "tipo_documento_fuente": "informe_supervision",
        "keywords_deteccion": ["informe de supervision", "informe supervision", "supervisor"],
        "orden": 60,
    },
    {
        "codigo": "DS_CONSECUTIVO",
        "etiqueta": "DS / Consecutivo de pago",
        "descripcion": "Consecutivo de pago de la entidad (opcional).",
        "obligatorio": False,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": "ds_consecutivo",
        "keywords_deteccion": ["ds-", "consecutivo"],
        "orden": 70,
    },
    {
        "codigo": "EVIDENCIAS",
        "etiqueta": "Evidencias por obligación",
        "descripcion": "Soportes (documentos, fotos, actas) de cada obligación.",
        "obligatorio": True,
        "solo_primera_cuenta": False,
        "permite_autogen": False,
        "tipo_documento_fuente": None,
        "keywords_deteccion": [],
        "orden": 80,
    },
    # First-only
    {
        "codigo": "CEDULA",
        "etiqueta": "Cédula de ciudadanía",
        "descripcion": "Copia de la cédula del contratista.",
        "obligatorio": True,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "cedula",
        "keywords_deteccion": ["cedula", "cédula"],
        "orden": 110,
    },
    {
        "codigo": "RUT",
        "etiqueta": "RUT",
        "descripcion": "Registro Único Tributario actualizado.",
        "obligatorio": True,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "rut",
        "keywords_deteccion": ["rut"],
        "orden": 120,
    },
    {
        "codigo": "FICHA_TECNICA",
        "etiqueta": "Ficha técnica",
        "descripcion": "Ficha técnica del contratista.",
        "obligatorio": False,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "ficha_tecnica",
        "keywords_deteccion": ["ficha tecnica", "ficha técnica"],
        "orden": 130,
    },
    {
        "codigo": "ACTA_INICIO",
        "etiqueta": "Acta de inicio",
        "descripcion": "Acta de inicio del contrato firmada.",
        "obligatorio": True,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "acta_inicio",
        "keywords_deteccion": ["acta de inicio", "acta inicio"],
        "orden": 140,
    },
    {
        "codigo": "DEPENDIENTES",
        "etiqueta": "Certificado de dependientes",
        "descripcion": "Declaración de dependientes económicos.",
        "obligatorio": False,
        "solo_primera_cuenta": True,
        "permite_autogen": False,
        "tipo_documento_fuente": "dependientes",
        "keywords_deteccion": ["dependientes"],
        "orden": 150,
    },
]


async def _seed_catalogo_si_vacio(db: AsyncSession) -> None:
    res = await db.execute(select(RequisitoDocumento).limit(1))
    if res.scalar_one_or_none() is not None:
        return
    for item in _CATALOGO_SEED:
        db.add(RequisitoDocumento(**item))
    await db.flush()


async def listar_catalogo(db: AsyncSession) -> list[RequisitoDocumento]:
    await _seed_catalogo_si_vacio(db)
    res = await db.execute(select(RequisitoDocumento).order_by(RequisitoDocumento.orden))
    return list(res.scalars().all())


# Default mode when a cuenta has not resolved the gate yet. Treating None as
# 'estandar' here keeps direct service callers (and the refresh/auto-link
# endpoints) working; the gate itself is enforced at the GET /checklist endpoint.
_MODO_DEFAULT = "estandar"


async def listar_requisitos_cuenta(db: AsyncSession, cuenta_id: uuid.UUID) -> list[RequisitoCuenta]:
    """Return the ACTIVE custom requirements defined for a cuenta, ordered."""
    res = await db.execute(
        select(RequisitoCuenta)
        .where(
            RequisitoCuenta.cuenta_cobro_id == cuenta_id,
            RequisitoCuenta.activo.is_(True),
        )
        .order_by(RequisitoCuenta.orden)
    )
    return list(res.scalars().all())


# ── core lifecycle ─────────────────────────────────────────────────────────


async def _is_first_cuenta(db: AsyncSession, cuenta: CuentaCobro) -> bool:
    """True if this cuenta has the smallest (anio, mes) for its contrato."""
    res = await db.execute(
        select(CuentaCobro)
        .where(
            CuentaCobro.contrato_id == cuenta.contrato_id,
            CuentaCobro.deleted_at.is_(None),
        )
        .order_by(CuentaCobro.anio.asc(), CuentaCobro.mes.asc())
        .limit(1)
    )
    first = res.scalar_one_or_none()
    return bool(first and first.id == cuenta.id)


def _codigos_estandar_a_crear(
    modo: str,
    catalogo: list[RequisitoDocumento],
    custom: list[RequisitoCuenta],
) -> set[str]:
    """Standard catalog codes that apply under the given build mode.

    - estandar / augment: the whole catalog.
    - reemplazar: only EVIDENCIAS (structural, tied to obligaciones) plus any
      standard code a custom item explicitly maps to (so e.g. a custom 'RUT'
      that maps to the standard still materialises the standard RUT row instead
      of being dropped with the rest of the catalog).
    """
    if modo == "reemplazar":
        mapeados = {c.mapea_a_estandar for c in custom if c.mapea_a_estandar}
        return {"EVIDENCIAS", *mapeados}
    return {req.codigo for req in catalogo}


async def asegurar_checklist(db: AsyncSession, cuenta: CuentaCobro) -> list[DocumentoCuentaCobro]:
    """Idempotent: ensure a DocumentoCuentaCobro row exists for every applicable
    requirement, merging the standard catalog with the cuenta's custom
    requisitos according to ``cuenta.requisitos_modo``:

    - ``estandar`` (or None): standard catalog only (legacy behaviour).
    - ``augment``: standard catalog + custom requisitos. A custom item that maps
      to a standard code is covered by the standard row (no duplicate).
    - ``reemplazar``: custom requisitos + EVIDENCIAS (and any standard a custom
      maps to); the rest of the catalog is dropped.

    New rows always start PENDIENTE — links are never copied from a previous
    cuenta. Returns the full list of rows (existing + newly created)."""
    modo = cuenta.requisitos_modo or _MODO_DEFAULT
    catalogo = await listar_catalogo(db)
    custom = await listar_requisitos_cuenta(db, cuenta.id)

    res = await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id))
    filas = list(res.scalars().all())
    por_codigo = {f.requisito_codigo: f for f in filas if f.requisito_codigo is not None}
    por_custom = {f.requisito_cuenta_id: f for f in filas if f.requisito_cuenta_id is not None}

    is_first = await _is_first_cuenta(db, cuenta)

    codigos_estandar = (
        _codigos_estandar_a_crear(modo, catalogo, custom) if modo != "estandar" else {req.codigo for req in catalogo}
    )

    creadas: list[DocumentoCuentaCobro] = []

    # Standard rows
    for req in catalogo:
        if req.codigo not in codigos_estandar:
            continue
        # Contract-level requisitos appear on EVERY cuenta (auto-fulfilled by the
        # shared contract-level document), so solo_primera_cuenta never hides them.
        if req.solo_primera_cuenta and not is_first and not es_nivel_contrato(req.codigo):
            continue
        if req.codigo in por_codigo:
            continue
        fila = DocumentoCuentaCobro(
            cuenta_cobro_id=cuenta.id,
            requisito_codigo=req.codigo,
            estado=EstadoRequisito.PENDIENTE,
        )
        db.add(fila)
        creadas.append(fila)

    # Custom rows (only when the mode includes custom requisitos). A custom item
    # mapped to a standard code is already covered by the standard row above.
    if modo in ("augment", "reemplazar"):
        for item in custom:
            if item.mapea_a_estandar:
                continue
            if item.solo_primera_cuenta and not is_first:
                continue
            if item.id in por_custom:
                continue
            fila = DocumentoCuentaCobro(
                cuenta_cobro_id=cuenta.id,
                requisito_cuenta_id=item.id,
                estado=EstadoRequisito.PENDIENTE,
            )
            db.add(fila)
            creadas.append(fila)

    if creadas:
        await db.flush()

    return [*filas, *creadas]


# ── SECOP detection ────────────────────────────────────────────────────────


def _num_matches_contrato(num: str, sc: SecopContrato) -> bool:
    """Whether a hand-entered contract number fuzzy-matches any SECOP contract identifier.

    In SECOP the user-facing number can live in `numero_contrato`, `referencia_del_contrato`
    (the CO1.PCCNTR.xxx code) or `proceso_de_compra`, and hand entry adds case/space/punct
    noise — so we compare against all three with tolerant similarity.
    """
    best = max(
        _similar(num, sc.numero_contrato),
        _similar(num, sc.referencia_del_contrato),
        _similar(num, sc.proceso_de_compra),
    )
    return best >= _SECOP_ID_SIMIL_THRESHOLD


async def _secop_documentos_del_contrato(db: AsyncSession, contrato: Contrato) -> list[SecopDocumento]:
    """Find the SECOP documents for a contract, tolerant of hand-entry differences.

    Matching is fuzzy and multi-identifier so a minor mismatch (case, spaces, accents,
    special characters, leading zeros) no longer yields zero results:
      1. Direct hit on the document's own `numero_contrato` / `proceso` (indexed, exact).
      2. Via the SECOP contract, anchored by the contractor's cedula/NIT (digits-only,
         leading-zero tolerant) and/or a fuzzy match on any contract-number field.
      3. Fallback fuzzy scan over a bounded pool when neither anchor resolves.
    """
    num = contrato.numero_contrato
    ced = contrato.documento_proveedor
    if not num and not ced:
        return []

    pool: dict[uuid.UUID, SecopDocumento] = {}

    # (1) Direct exact/normalized-equal hit on the document identifiers.
    if num:
        direct = await db.execute(
            select(SecopDocumento).where(or_(SecopDocumento.numero_contrato == num, SecopDocumento.proceso == num))
        )
        for d in direct.scalars().all():
            pool[d.id] = d

    # (2) Via SecopContrato, anchored by cedula (digits) and/or contract number.
    ced_key = _solo_digitos(ced)
    conds: list[ColumnElement[bool]] = []
    if ced_key:
        conds.append(SecopContrato.cedula_contratista.like(f"%{ced_key}%"))
    if num:
        conds.extend(
            [
                SecopContrato.numero_contrato == num,
                SecopContrato.referencia_del_contrato == num,
                SecopContrato.proceso_de_compra == num,
            ]
        )
    matched_sc_ids: set[uuid.UUID] = set()
    if conds:
        sc_rows = (await db.execute(select(SecopContrato).where(or_(*conds)))).scalars().all()
        for sc in sc_rows:
            ced_ok = bool(ced_key) and _solo_digitos(sc.cedula_contratista) == ced_key
            num_ok = bool(num) and _num_matches_contrato(num, sc)
            if ced_ok or num_ok:
                matched_sc_ids.add(sc.id)
    if matched_sc_ids:
        via_sc = await db.execute(select(SecopDocumento).where(SecopDocumento.secop_contrato_id.in_(matched_sc_ids)))
        for d in via_sc.scalars().all():
            pool[d.id] = d

    # (3) Fallback: no anchor resolved (typo in the number and no cedula link).
    # Fuzzy-scan a bounded pool of documents that carry any identifier.
    if not pool and num:
        scan = await db.execute(
            select(SecopDocumento)
            .where(or_(SecopDocumento.numero_contrato.isnot(None), SecopDocumento.proceso.isnot(None)))
            .limit(_SECOP_FUZZY_SCAN_LIMIT)
        )
        candidatos = scan.scalars().all()
        if len(candidatos) >= _SECOP_FUZZY_SCAN_LIMIT:
            await logger.awarning(
                "secop_fuzzy_scan_capped", contrato_id=str(contrato.id), limit=_SECOP_FUZZY_SCAN_LIMIT
            )
        for d in candidatos:
            best = max(_similar(num, d.numero_contrato), _similar(num, d.proceso))
            if best >= _SECOP_ID_SIMIL_THRESHOLD:
                pool[d.id] = d

    return list(pool.values())


async def detectar_desde_secop(
    db: AsyncSession, cuenta: CuentaCobro
) -> dict[str, list[tuple[SecopDocumento, Decimal]]]:
    """Scan secop_documentos for the contract and score each requisito.

    Primary signal: persistent categoria on SecopDocumento (set by document_classifier).
    Fallback: keyword scoring against requisito.keywords_deteccion for requisitos that
    have no category mapping (e.g. INFORME_*, DS_CONSECUTIVO, FICHA_TECNICA).

    Persists top-N candidates per requisito in `documento_checklist_candidatos`.
    Auto-links the best match (estado=DETECTADO) when score >= AUTO_LINK_THRESHOLD
    and the checklist row is still PENDIENTE.

    Returns a dict {requisito_codigo: [(secop_doc, score), ...]} (top-N each).
    """
    contrato_res = await db.execute(select(Contrato).where(Contrato.id == cuenta.contrato_id))
    contrato = contrato_res.scalar_one()

    secop_docs = await _secop_documentos_del_contrato(db, contrato)

    all_rows = list(
        (await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id)))
        .scalars()
        .all()
    )
    # Standard catalog rows are matched by categoria + keywords; custom rows (below)
    # are matched by their own keywords_deteccion only.
    rows = {r.requisito_codigo: r for r in all_rows if r.requisito_codigo is not None}
    custom_rows = {r.requisito_cuenta_id: r for r in all_rows if r.requisito_cuenta_id is not None}

    cat = await listar_catalogo(db)

    await db.execute(
        DocumentoChecklistCandidato.__table__.delete().where(DocumentoChecklistCandidato.cuenta_cobro_id == cuenta.id)
    )

    # Build category-based scores: {requisito_codigo: [(doc, score)]}
    categoria_candidatos: dict[str, list[tuple[SecopDocumento, Decimal]]] = {}
    for doc in secop_docs:
        req_codigo = CATEGORIA_A_REQUISITO.get(doc.categoria)
        if req_codigo is None or req_codigo not in rows:
            continue
        # Manual override → confidence 1.000; automatic → stored confianza or 0.500 floor
        if doc.categoria_override:
            score = Decimal("1.000")
        else:
            score = Decimal(f"{doc.categoria_confianza:.3f}") if doc.categoria_confianza else Decimal("0.500")
        categoria_candidatos.setdefault(req_codigo, []).append((doc, score))

    resultado: dict[str, list[tuple[SecopDocumento, Decimal]]] = {}

    for req in cat:
        if req.codigo not in rows:
            continue

        scored: list[tuple[SecopDocumento, Decimal]] = []

        # Primary: category-based candidates for this requisito
        cat_scored = categoria_candidatos.get(req.codigo, [])
        scored.extend(cat_scored)

        # Fallback: keyword scoring for docs not already captured by category
        cat_doc_ids = {d.id for d, _ in cat_scored}
        if req.keywords_deteccion:
            for doc in secop_docs:
                if doc.id in cat_doc_ids:
                    continue
                kw_score = _keyword_score([doc.nombre_archivo, doc.descripcion], req.keywords_deteccion)
                if kw_score > 0:
                    scored.append((doc, kw_score))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:TOP_N_CANDIDATES]
        resultado[req.codigo] = top

        for doc, score in top:
            db.add(
                DocumentoChecklistCandidato(
                    cuenta_cobro_id=cuenta.id,
                    requisito_codigo=req.codigo,
                    secop_documento_id=doc.id,
                    score=score,
                )
            )

        fila = rows[req.codigo]
        if (
            top
            and fila.estado == EstadoRequisito.PENDIENTE
            and fila.documento_fuente_id is None
            and fila.secop_documento_id is None
            and top[0][1] >= AUTO_LINK_THRESHOLD
        ):
            fila.secop_documento_id = top[0][0].id
            fila.confianza_deteccion = top[0][1]
            fila.estado = EstadoRequisito.DETECTADO
            db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, secop_documento_id=top[0][0].id))

    # Custom (per-cuenta) requisitos: keyword scoring against their own keywords.
    # Candidates are not persisted (DocumentoChecklistCandidato FK targets the standard
    # catalog); they are recomputed on the fly for display in construir_checklist_completo.
    # Conservative auto-link: only the top match at/above the standard threshold.
    if custom_rows:
        customs = await listar_requisitos_cuenta(db, cuenta.id)
        for item in customs:
            fila = custom_rows.get(item.id)
            if fila is None or not item.keywords_deteccion:
                continue
            scored = [
                (doc, _keyword_score([doc.nombre_archivo, doc.descripcion], item.keywords_deteccion))
                for doc in secop_docs
            ]
            scored = [(d, s) for d, s in scored if s > 0]
            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:TOP_N_CANDIDATES]
            resultado[str(item.id)] = top
            if (
                top
                and fila.estado == EstadoRequisito.PENDIENTE
                and fila.documento_fuente_id is None
                and fila.secop_documento_id is None
                and top[0][1] >= AUTO_LINK_THRESHOLD
            ):
                fila.secop_documento_id = top[0][0].id
                fila.confianza_deteccion = top[0][1]
                fila.estado = EstadoRequisito.DETECTADO
                db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, secop_documento_id=top[0][0].id))

    await db.flush()
    return resultado


# ── manual transitions ─────────────────────────────────────────────────────


async def _get_fila(db: AsyncSession, cuenta_id: uuid.UUID, requisito_ref: str) -> DocumentoCuentaCobro:
    """Resolve a checklist row by its public reference.

    ``requisito_ref`` is a standard catalog code (e.g. "RUT") for standard rows,
    or the ``requisito_cuenta_id`` UUID (as a string) for custom rows.
    """
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.requisito_codigo == requisito_ref,
        )
    )
    fila = res.scalar_one_or_none()
    if fila is not None:
        return fila

    # Fall back to a custom requisito addressed by UUID.
    try:
        ref_uuid = uuid.UUID(requisito_ref)
    except ValueError:
        ref_uuid = None
    if ref_uuid is not None:
        res = await db.execute(
            select(DocumentoCuentaCobro).where(
                DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
                DocumentoCuentaCobro.requisito_cuenta_id == ref_uuid,
            )
        )
        fila = res.scalar_one_or_none()
        if fila is not None:
            return fila

    raise NotFoundError(
        "DocumentoCuentaCobro",
        f"cuenta={cuenta_id} requisito={requisito_ref}",
    )


def _estado_segun_vinculos(fila: DocumentoCuentaCobro) -> EstadoRequisito:
    """Derive estado from what's currently in the primary slots.

    A user-uploaded document always outranks a SECOP detection: if a
    documento_fuente is present (primary slot non-null implies at least one
    vinculo of that kind exists) the row is CARGADO, regardless of a
    secop_documento also being linked (mixed sources may coexist).
    """
    if fila.documento_fuente_id is not None:
        return EstadoRequisito.CARGADO
    if fila.secop_documento_id is not None:
        return EstadoRequisito.DETECTADO
    return EstadoRequisito.PENDIENTE


async def vincular_documento_fuente(
    db: AsyncSession,
    cuenta_id: uuid.UUID,
    requisito_codigo: str,
    documento_fuente_id: uuid.UUID,
) -> DocumentoCuentaCobro:
    """Link a user-uploaded DocumentoFuente to a checklist row.

    Appends: creates a new vinculo row (idempotent — linking the same document
    twice is a no-op) and, only if the primary slot is empty, promotes it to
    primary. An existing non-empty primary slot is NEVER overwritten — that was
    the previous last-write-wins data-loss bug. A requisito may therefore end up
    with several linked documents (e.g. RPC original + RPC de adición).
    """
    fila = await _get_fila(db, cuenta_id, requisito_codigo)
    doc_res = await db.execute(select(DocumentoFuente).where(DocumentoFuente.id == documento_fuente_id))
    if doc_res.scalar_one_or_none() is None:
        raise NotFoundError("DocumentoFuente", str(documento_fuente_id))

    ya_vinculado = await db.execute(
        select(DocumentoRequisitoVinculo).where(
            DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id,
            DocumentoRequisitoVinculo.documento_fuente_id == documento_fuente_id,
        )
    )
    if ya_vinculado.scalar_one_or_none() is None:
        try:
            async with db.begin_nested():
                db.add(
                    DocumentoRequisitoVinculo(
                        documento_cuenta_cobro_id=fila.id, documento_fuente_id=documento_fuente_id
                    )
                )
                await db.flush()
        except IntegrityError:
            # A concurrent request already inserted the same vinculo between our
            # SELECT and INSERT — idempotent no-op. Rolling back to the savepoint
            # keeps the outer transaction usable.
            pass
        else:
            if fila.documento_fuente_id is None:
                fila.documento_fuente_id = documento_fuente_id

    fila.estado = _estado_segun_vinculos(fila)
    await db.flush()
    return fila


async def vincular_secop_documento(
    db: AsyncSession,
    cuenta_id: uuid.UUID,
    requisito_codigo: str,
    secop_documento_id: uuid.UUID,
) -> DocumentoCuentaCobro:
    """Link a SECOP cached document to a checklist row.

    Same append semantics as ``vincular_documento_fuente``: a new vinculo row is
    created (idempotent) and only promoted to primary if that slot is empty.
    No longer clears ``documento_fuente_id`` — a checklist row can hold both an
    uploaded document and a SECOP-detected one at the same time.
    """
    fila = await _get_fila(db, cuenta_id, requisito_codigo)
    doc_res = await db.execute(select(SecopDocumento).where(SecopDocumento.id == secop_documento_id))
    if doc_res.scalar_one_or_none() is None:
        raise NotFoundError("SecopDocumento", str(secop_documento_id))

    ya_vinculado = await db.execute(
        select(DocumentoRequisitoVinculo).where(
            DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id,
            DocumentoRequisitoVinculo.secop_documento_id == secop_documento_id,
        )
    )
    if ya_vinculado.scalar_one_or_none() is None:
        try:
            async with db.begin_nested():
                db.add(
                    DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, secop_documento_id=secop_documento_id)
                )
                await db.flush()
        except IntegrityError:
            # A concurrent request already inserted the same vinculo between our
            # SELECT and INSERT — idempotent no-op.
            pass
        else:
            if fila.secop_documento_id is None:
                fila.secop_documento_id = secop_documento_id

    # confianza_deteccion is informational; clear unless we look it up
    fila.confianza_deteccion = None
    fila.estado = _estado_segun_vinculos(fila)
    await db.flush()
    return fila


async def desvincular(
    db: AsyncSession,
    cuenta_id: uuid.UUID,
    requisito_codigo: str,
    *,
    documento_fuente_id: uuid.UUID | None = None,
    secop_documento_id: uuid.UUID | None = None,
    vinculo_id: uuid.UUID | None = None,
) -> DocumentoCuentaCobro:
    """Remove link(s) from a checklist row.

    No args (legacy behaviour): removes EVERY link and resets to PENDIENTE.
    With ``documento_fuente_id`` / ``secop_documento_id`` / ``vinculo_id``: removes
    only that ONE specific link. If it was the primary link, the oldest remaining
    vinculo of the same kind (documento_fuente or secop_documento) is promoted
    into the primary slot; if none remain, that primary slot is cleared. estado
    is always recomputed from what remains (PENDIENTE only when nothing is left).
    """
    fila = await _get_fila(db, cuenta_id, requisito_codigo)

    if documento_fuente_id is None and secop_documento_id is None and vinculo_id is None:
        await db.execute(
            DocumentoRequisitoVinculo.__table__.delete().where(
                DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id
            )
        )
        fila.documento_fuente_id = None
        fila.secop_documento_id = None
        fila.confianza_deteccion = None
        fila.estado = EstadoRequisito.PENDIENTE
        await db.flush()
        return fila

    stmt = select(DocumentoRequisitoVinculo).where(DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id)
    if vinculo_id is not None:
        stmt = stmt.where(DocumentoRequisitoVinculo.id == vinculo_id)
    elif documento_fuente_id is not None:
        stmt = stmt.where(DocumentoRequisitoVinculo.documento_fuente_id == documento_fuente_id)
    else:
        stmt = stmt.where(DocumentoRequisitoVinculo.secop_documento_id == secop_documento_id)

    vinculo = (await db.execute(stmt)).scalar_one_or_none()
    if vinculo is None:
        # Nothing to remove — idempotent no-op.
        return fila

    era_fuente = vinculo.documento_fuente_id is not None
    era_primaria = (era_fuente and fila.documento_fuente_id == vinculo.documento_fuente_id) or (
        not era_fuente and fila.secop_documento_id == vinculo.secop_documento_id
    )

    await db.delete(vinculo)
    await db.flush()

    if era_primaria:
        kind_col = (
            DocumentoRequisitoVinculo.documento_fuente_id
            if era_fuente
            else DocumentoRequisitoVinculo.secop_documento_id
        )
        siguiente = (
            (
                await db.execute(
                    select(DocumentoRequisitoVinculo)
                    .where(
                        DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id,
                        kind_col.isnot(None),
                    )
                    .order_by(DocumentoRequisitoVinculo.created_at.asc())
                )
            )
            .scalars()
            .first()
        )
        if era_fuente:
            fila.documento_fuente_id = siguiente.documento_fuente_id if siguiente else None
        else:
            fila.secop_documento_id = siguiente.secop_documento_id if siguiente else None

    if fila.documento_fuente_id is None and fila.secop_documento_id is None:
        fila.confianza_deteccion = None
    # Only recompute estado from the remaining links when it was already in an
    # auto-derived state. A manual override (CUMPLIDO_MANUAL/NO_APLICA) must survive
    # unlinking a single document — the user's manual decision is not tied to any
    # particular link and removing ONE of several must not silently revert it.
    if fila.estado in (EstadoRequisito.CARGADO, EstadoRequisito.DETECTADO, EstadoRequisito.PENDIENTE):
        fila.estado = _estado_segun_vinculos(fila)
    await db.flush()
    return fila


async def marcar_no_aplica(db: AsyncSession, cuenta_id: uuid.UUID, requisito_codigo: str) -> DocumentoCuentaCobro:
    fila = await _get_fila(db, cuenta_id, requisito_codigo)
    fila.estado = EstadoRequisito.NO_APLICA
    await db.flush()
    return fila


async def marcar_cumplido_manual(db: AsyncSession, cuenta_id: uuid.UUID, requisito_codigo: str) -> DocumentoCuentaCobro:
    fila = await _get_fila(db, cuenta_id, requisito_codigo)
    fila.estado = EstadoRequisito.CUMPLIDO_MANUAL
    await db.flush()
    return fila


async def set_observaciones(
    db: AsyncSession,
    cuenta_id: uuid.UUID,
    requisito_codigo: str,
    observaciones: str | None,
) -> DocumentoCuentaCobro:
    fila = await _get_fila(db, cuenta_id, requisito_codigo)
    fila.observaciones = observaciones
    await db.flush()
    return fila


# Minimum score to auto-link an uploaded document to a checklist row.
# Lower than SECOP threshold (0.700) because tipo signal alone yields 0.750
# and we want to cover requisitos without CategoriaDocumento.
_AUTO_LINK_FUENTE_THRESHOLD = Decimal("0.700")


def _score_fuente_para_requisito(doc: DocumentoFuente, req_codigo: str) -> Decimal:
    """Multi-signal score (0.000-1.000) for assigning a DocumentoFuente to a checklist row.

    Signal priority (highest score wins):
      1. categoria_override=True + categoria maps to req_codigo → 1.000
      2. categoria != OTROS + maps to req_codigo → categoria_confianza (or 0.500 floor)
      3. tipo declared by user maps to req_codigo → 0.750

    When signals 2 and 3 both fire (categoria + tipo both match), the higher score wins,
    which means a high-confidence categoria classification beats the fixed tipo score.
    """
    best = Decimal("0.000")

    # Signals 1 & 2: semantic category (classifier output)
    if doc.categoria and doc.categoria != CategoriaDocumento.OTROS:
        cat_req = CATEGORIA_A_REQUISITO.get(doc.categoria)
        if cat_req == req_codigo:
            if doc.categoria_override:
                return Decimal("1.000")
            conf = Decimal(f"{doc.categoria_confianza:.3f}") if doc.categoria_confianza else Decimal("0.500")
            best = max(best, conf)

    # Signal 3: user-declared tipo (reliable declarative intent)
    tipo_val = doc.tipo.value if hasattr(doc.tipo, "value") else str(doc.tipo)
    if TIPO_A_REQUISITO.get(tipo_val) == req_codigo:
        best = max(best, Decimal("0.750"))

    return best


async def auto_vincular_documentos_fuente(
    db: AsyncSession,
    cuenta: CuentaCobro,
) -> int:
    """Auto-link DocumentoFuente to PENDIENTE checklist rows using multi-signal scoring.

    For each PENDIENTE requisito, evaluates all uploaded documents and links the
    highest-scoring candidate that meets the auto-link threshold (≥ 0.700).

    Scoring signals per (document, requisito) pair:
      - categoria_override=True + categoria matches → 1.000 (absolute trust)
      - categoria != OTROS + matches → categoria_confianza (semantic classifier output)
      - tipo declared by user matches → 0.750 (covers INFORME_*, COMPROBANTE_*, etc.)

    When multiple documents match the same requisito, the one with the highest composite
    score wins. Ties broken by: categoria_override=True first, then highest confianza.

    Only PENDIENTE rows are touched — manual links (CARGADO/DETECTADO) are never overwritten.
    Returns the number of newly linked rows.
    """
    # Two document pools (two-tier model):
    #   - docs_contrato: shared contract-level documents (cuenta_cobro_id IS NULL)
    #     that auto-fulfil contract-level requisitos in every cuenta.
    #   - docs_cuenta: documents that belong strictly to THIS cuenta.
    docs_cuenta = list(
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.cuenta_cobro_id == cuenta.id))).scalars().all()
    )
    docs_contrato = list(
        (
            await db.execute(
                select(DocumentoFuente).where(
                    DocumentoFuente.contrato_id == cuenta.contrato_id,
                    DocumentoFuente.cuenta_cobro_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    def _pool_para(req_codigo: str) -> list[DocumentoFuente]:
        return docs_contrato if es_nivel_contrato(req_codigo) else docs_cuenta

    rows_res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
        )
    )
    all_rows = list(rows_res.scalars().all())
    # Standard rows auto-link by categoria/tipo; custom rows (below) auto-link by
    # their own keywords_deteccion against the uploaded document text.
    rows = {r.requisito_codigo: r for r in all_rows if r.requisito_codigo is not None}
    custom_rows = {r.requisito_cuenta_id: r for r in all_rows if r.requisito_cuenta_id is not None}

    # Self-heal: reset any row whose linked document no longer belongs to the pool
    # for that row's tier (contract-level vs cuenta-level).
    ids_cuenta = {d.id for d in docs_cuenta}
    ids_contrato = {d.id for d in docs_contrato}
    reparados = 0
    for req_codigo, fila in rows.items():
        if fila.documento_fuente_id is None:
            continue
        pool_ids = ids_contrato if es_nivel_contrato(req_codigo) else ids_cuenta
        if fila.documento_fuente_id not in pool_ids:
            await db.execute(
                DocumentoRequisitoVinculo.__table__.delete().where(
                    DocumentoRequisitoVinculo.documento_cuenta_cobro_id == fila.id,
                    DocumentoRequisitoVinculo.documento_fuente_id == fila.documento_fuente_id,
                )
            )
            fila.documento_fuente_id = None
            fila.confianza_deteccion = None
            fila.estado = EstadoRequisito.PENDIENTE
            reparados += 1

    if not docs_cuenta and not docs_contrato:
        if reparados:
            await db.flush()
        return 0

    # Build candidate list per requisito: [(score, override, confianza, doc)] from
    # the row's tier pool. Sort key: score DESC, override DESC, confianza DESC.
    candidates: dict[str, list[tuple[Decimal, bool, float, DocumentoFuente]]] = {}
    for req_codigo, fila in rows.items():
        if fila.estado != EstadoRequisito.PENDIENTE:
            continue
        for doc in _pool_para(req_codigo):
            score = _score_fuente_para_requisito(doc, req_codigo)
            if score >= _AUTO_LINK_FUENTE_THRESHOLD:
                candidates.setdefault(req_codigo, []).append(
                    (score, doc.categoria_override, doc.categoria_confianza or 0.0, doc)
                )

    vinculados = 0
    for req_codigo, cands in candidates.items():
        fila = rows[req_codigo]
        if fila.estado != EstadoRequisito.PENDIENTE:
            continue
        cands.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
        best_score, _, _, best_doc = cands[0]
        db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=best_doc.id))
        fila.documento_fuente_id = best_doc.id
        fila.confianza_deteccion = best_score
        fila.estado = EstadoRequisito.CARGADO
        vinculados += 1

    # Custom rows: score uploaded documents by the requisito's own keywords against
    # the document name + extracted text. Conservative auto-link: only the best match
    # at/above the threshold; weaker matches surface as candidates in the checklist.
    if custom_rows:
        customs = await listar_requisitos_cuenta(db, cuenta.id)
        for item in customs:
            fila = custom_rows.get(item.id)
            if fila is None or fila.estado != EstadoRequisito.PENDIENTE or not item.keywords_deteccion:
                continue
            best_doc = None
            best_score = Decimal("0.000")
            for doc in docs_cuenta:
                score = _keyword_score([doc.nombre, doc.texto_extraido], item.keywords_deteccion)
                if score > best_score:
                    best_score, best_doc = score, doc
            if best_doc is not None and best_score >= _AUTO_LINK_FUENTE_THRESHOLD:
                db.add(DocumentoRequisitoVinculo(documento_cuenta_cobro_id=fila.id, documento_fuente_id=best_doc.id))
                fila.documento_fuente_id = best_doc.id
                fila.confianza_deteccion = best_score
                fila.estado = EstadoRequisito.CARGADO
                vinculados += 1

    await logger.ainfo(
        "auto_vincular_resultado",
        cuenta_id=str(cuenta.id),
        vinculados=vinculados,
        requisitos_vinculados=list(candidates.keys()) if vinculados else [],
    )

    if vinculados or reparados:
        await db.flush()

    return vinculados


# ── logical evidence tree ──────────────────────────────────────────────────


_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


async def listar_arbol_evidencias(db: AsyncSession, cuenta: CuentaCobro) -> list[dict]:
    """Build a logical evidence tree grouped by obligación.

    Returns a list of dicts, one per obligación of the contract, with its letter
    (A, B, …), descripción, the actividades of this cuenta linked to it, and
    each actividad's evidencias.
    """
    # Obligaciones of the contract (ordered)
    obl_res = await db.execute(
        select(Obligacion)
        .where(Obligacion.contrato_id == cuenta.contrato_id)
        .order_by(Obligacion.orden.asc(), Obligacion.created_at.asc())
    )
    obligaciones = list(obl_res.scalars().all())

    # Actividades of the cuenta with evidencias
    act_res = await db.execute(
        select(Actividad).options(selectinload(Actividad.evidencias)).where(Actividad.cuenta_cobro_id == cuenta.id)
    )
    actividades = list(act_res.scalars().all())
    by_obligacion: dict[uuid.UUID | None, list[Actividad]] = {}
    for act in actividades:
        by_obligacion.setdefault(act.obligacion_id, []).append(act)

    arbol: list[dict] = []
    for idx, obl in enumerate(obligaciones):
        letra = _LETTERS[idx] if idx < len(_LETTERS) else f"X{idx}"
        acts = by_obligacion.get(obl.id, [])
        arbol.append(
            {
                "obligacion_id": str(obl.id),
                "letra": letra,
                "descripcion": obl.descripcion,
                "tipo": obl.tipo.value if obl.tipo else None,
                "actividades": [
                    {
                        "id": str(a.id),
                        "descripcion": a.descripcion,
                        "evidencias": [
                            {
                                "id": str(e.id),
                                "nombre_archivo": e.nombre_archivo,
                                "tipo_archivo": e.tipo_archivo,
                                "tamano_bytes": e.tamano_bytes,
                            }
                            for e in a.evidencias
                        ],
                    }
                    for a in acts
                ],
            }
        )
    return arbol


# ── summary ────────────────────────────────────────────────────────────────


def _fila_obligatorio_y_ref(
    fila: DocumentoCuentaCobro,
    cat_by_codigo: dict[str, RequisitoDocumento],
    custom_by_id: dict[uuid.UUID, RequisitoCuenta],
) -> tuple[bool, str] | None:
    """Return (obligatorio, public_ref) for a checklist row, or None if its
    definition cannot be resolved (e.g. an orphaned/custom-disabled row)."""
    if fila.requisito_codigo is not None:
        req = cat_by_codigo.get(fila.requisito_codigo)
        if req is None:
            return None
        return req.obligatorio, fila.requisito_codigo
    if fila.requisito_cuenta_id is not None:
        item = custom_by_id.get(fila.requisito_cuenta_id)
        if item is None:
            return None
        return item.obligatorio, str(fila.requisito_cuenta_id)
    return None


def computar_resumen(
    filas: list[DocumentoCuentaCobro],
    catalogo: list[RequisitoDocumento],
    custom_by_id: dict[uuid.UUID, RequisitoCuenta] | None = None,
) -> dict:
    cat_by_codigo = {c.codigo: c for c in catalogo}
    custom_by_id = custom_by_id or {}
    total = 0
    cumplidos = 0
    pendientes: list[str] = []
    for fila in filas:
        meta = _fila_obligatorio_y_ref(fila, cat_by_codigo, custom_by_id)
        if meta is None:
            continue
        obligatorio, ref = meta
        if not obligatorio:
            continue
        if fila.estado == EstadoRequisito.NO_APLICA:
            continue
        total += 1
        if fila.estado in (
            EstadoRequisito.CARGADO,
            EstadoRequisito.DETECTADO,
            EstadoRequisito.CUMPLIDO_MANUAL,
        ):
            cumplidos += 1
        else:
            pendientes.append(ref)
    return {
        "total": total,
        "cumplidos": cumplidos,
        "pendientes": len(pendientes),
        "lista_pendientes": pendientes,
        "radicacion_lista": len(pendientes) == 0 and total > 0,
    }


# ── assemble full response payload ─────────────────────────────────────────


def _ref_documento_fuente(d: DocumentoFuente) -> dict:
    return {
        "id": d.id,
        "nombre": d.nombre,
        "tipo": d.tipo.value if hasattr(d.tipo, "value") else str(d.tipo),
        "categoria": d.categoria.value if d.categoria else None,
        "categoria_confianza": d.categoria_confianza,
        "categoria_override": d.categoria_override,
    }


def _ref_secop_documento(d: SecopDocumento) -> dict:
    return {
        "id": d.id,
        "nombre_archivo": d.nombre_archivo,
        "descripcion": d.descripcion,
        "url_descarga": d.url_descarga,
        "categoria": d.categoria.value if d.categoria else None,
        "categoria_confianza": d.categoria_confianza,
        "categoria_override": d.categoria_override,
    }


def _todos_los_documentos_fuente(fila: DocumentoCuentaCobro) -> list[dict]:
    """All linked DocumentoFuente for this row, primary first, then the rest of
    the vinculos ordered by created_at (oldest first)."""
    vistos: set[uuid.UUID] = set()
    resultado: list[dict] = []
    if fila.documento_fuente is not None:
        resultado.append(_ref_documento_fuente(fila.documento_fuente))
        vistos.add(fila.documento_fuente.id)
    for v in fila.vinculos:
        if v.documento_fuente is not None and v.documento_fuente.id not in vistos:
            resultado.append(_ref_documento_fuente(v.documento_fuente))
            vistos.add(v.documento_fuente.id)
    return resultado


def _todos_los_secop_documentos(fila: DocumentoCuentaCobro) -> list[dict]:
    """All linked SecopDocumento for this row, primary first, then the rest of
    the vinculos ordered by created_at (oldest first)."""
    vistos: set[uuid.UUID] = set()
    resultado: list[dict] = []
    if fila.secop_documento is not None:
        resultado.append(_ref_secop_documento(fila.secop_documento))
        vistos.add(fila.secop_documento.id)
    for v in fila.vinculos:
        if v.secop_documento is not None and v.secop_documento.id not in vistos:
            resultado.append(_ref_secop_documento(v.secop_documento))
            vistos.add(v.secop_documento.id)
    return resultado


async def construir_checklist_completo(
    db: AsyncSession,
    cuenta: CuentaCobro,
    *,
    auto_vincular: bool = False,
) -> dict:
    """Build the full checklist response (items + candidatos + resumen + arbol).

    Idempotently ensures rows exist; does NOT re-scan SECOP (call
    `detectar_desde_secop` explicitly via /refresh-secop) and does NOT
    auto-link documents (call `auto_vincular_documentos_fuente` explicitly
    via /auto-vincular-documentos).

    Pass auto_vincular=True only when the caller explicitly wants to run
    the auto-link pass (e.g. from the /auto-vincular-documentos endpoint).
    Keeping this False by default prevents GET requests from silently filling
    PENDIENTE rows, which would block SECOP detection on /refresh-secop.
    """
    await asegurar_checklist(db, cuenta)
    if auto_vincular:
        await auto_vincular_documentos_fuente(db, cuenta)

    catalogo = await listar_catalogo(db)
    cat_by_codigo = {c.codigo: c for c in catalogo}
    custom_list = await listar_requisitos_cuenta(db, cuenta.id)
    custom_by_id = {c.id: c for c in custom_list}

    rows_res = await db.execute(
        select(DocumentoCuentaCobro)
        .options(
            selectinload(DocumentoCuentaCobro.documento_fuente),
            selectinload(DocumentoCuentaCobro.secop_documento),
            selectinload(DocumentoCuentaCobro.vinculos).selectinload(DocumentoRequisitoVinculo.documento_fuente),
            selectinload(DocumentoCuentaCobro.vinculos).selectinload(DocumentoRequisitoVinculo.secop_documento),
        )
        .where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id)
    )
    filas = list(rows_res.scalars().all())

    cand_res = await db.execute(
        select(DocumentoChecklistCandidato)
        .options(selectinload(DocumentoChecklistCandidato.secop_documento))
        .where(DocumentoChecklistCandidato.cuenta_cobro_id == cuenta.id)
        .order_by(DocumentoChecklistCandidato.score.desc())
    )
    candidatos = list(cand_res.scalars().all())
    cand_by_req: dict[str, list[DocumentoChecklistCandidato]] = {}
    for c in candidatos:
        cand_by_req.setdefault(c.requisito_codigo, []).append(c)

    # Fetch uploaded-document candidates for this contract.
    # Load ALL docs linked to this contract (not just by tipo) so we can also
    # match by categoria (which is more semantic and reliable than tipo).
    contrato_res = await db.execute(select(Contrato).where(Contrato.id == cuenta.contrato_id))
    contrato = contrato_res.scalar_one()
    # SECOP docs for the contract (fuzzy-matched) — used to compute on-the-fly
    # candidates for custom requisitos, whose candidates are NOT persisted in
    # documento_checklist_candidatos (that table's FK targets the standard catalog).
    secop_docs_contrato = await _secop_documentos_del_contrato(db, contrato)
    # Two document pools (two-tier model): shared contract-level docs (cuenta_cobro_id
    # IS NULL) that satisfy contract-level requisitos in every cuenta, and this cuenta's
    # own docs. Candidates per row are drawn from the pool matching the requisito's tier.
    docs_cuenta: list[DocumentoFuente] = list(
        (await db.execute(select(DocumentoFuente).where(DocumentoFuente.cuenta_cobro_id == cuenta.id))).scalars().all()
    )
    docs_contrato: list[DocumentoFuente] = list(
        (
            await db.execute(
                select(DocumentoFuente).where(
                    DocumentoFuente.contrato_id == cuenta.contrato_id,
                    DocumentoFuente.cuenta_cobro_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )

    def _index_docs(
        docs: list[DocumentoFuente],
    ) -> tuple[dict[str, list[DocumentoFuente]], dict[str, list[DocumentoFuente]]]:
        by_tipo: dict[str, list[DocumentoFuente]] = {}
        by_cat: dict[str, list[DocumentoFuente]] = {}
        for doc in docs:
            tipo_val = doc.tipo.value if hasattr(doc.tipo, "value") else str(doc.tipo)
            by_tipo.setdefault(tipo_val, []).append(doc)
            if doc.categoria is not None:
                cat_val = doc.categoria.value if hasattr(doc.categoria, "value") else str(doc.categoria)
                by_cat.setdefault(cat_val, []).append(doc)
        return by_tipo, by_cat

    tipo_cuenta, cat_cuenta = _index_docs(docs_cuenta)
    tipo_contrato, cat_contrato = _index_docs(docs_contrato)

    def _orden_de(fila: DocumentoCuentaCobro) -> int:
        if fila.requisito_codigo in cat_by_codigo:
            return cat_by_codigo[fila.requisito_codigo].orden
        if fila.requisito_cuenta_id in custom_by_id:
            return custom_by_id[fila.requisito_cuenta_id].orden
        return 999

    items: list[dict] = []
    for fila in sorted(filas, key=_orden_de):
        # Resolve the requisito definition from the standard catalog OR the
        # custom per-cuenta set, building a uniform `requisito` block.
        if fila.requisito_codigo is not None:
            req = cat_by_codigo.get(fila.requisito_codigo)
            if req is None:
                continue
            requisito_block = {
                "codigo": req.codigo,
                "etiqueta": req.etiqueta,
                "descripcion": req.descripcion,
                "obligatorio": req.obligatorio,
                "solo_primera_cuenta": req.solo_primera_cuenta,
                "permite_autogen": req.permite_autogen,
                "tipo_documento_fuente": req.tipo_documento_fuente,
                "orden": req.orden,
                "origen": "estandar",
                "requisito_cuenta_id": None,
            }
            tipo_req = req.tipo_documento_fuente
            req_cat_enum = _REQUISITO_A_CATEGORIA.get(req.codigo)
            req_cat_val = (
                (req_cat_enum.value if hasattr(req_cat_enum, "value") else str(req_cat_enum)) if req_cat_enum else None
            )
            # Normalize to (secop_documento_id, secop_documento, score) tuples so the
            # display logic is uniform with the custom (computed) branch below.
            candidatos_secop_src = [
                (c.secop_documento_id, c.secop_documento, c.score) for c in cand_by_req.get(fila.requisito_codigo, [])
            ]
        else:
            item_def = custom_by_id.get(fila.requisito_cuenta_id)
            if item_def is None:
                continue
            requisito_block = {
                "codigo": item_def.codigo,
                "etiqueta": item_def.etiqueta,
                "descripcion": item_def.descripcion,
                "obligatorio": item_def.obligatorio,
                "solo_primera_cuenta": item_def.solo_primera_cuenta,
                "permite_autogen": False,
                "tipo_documento_fuente": item_def.tipo_documento_fuente,
                "orden": item_def.orden,
                "origen": "cuenta",
                "requisito_cuenta_id": fila.requisito_cuenta_id,
            }
            tipo_req = item_def.tipo_documento_fuente
            req_cat_val = None
            # Custom SECOP candidates: keyword-scored on the fly (not persisted).
            candidatos_secop_src = []
            if item_def.keywords_deteccion and secop_docs_contrato:
                scored = [
                    (d, _keyword_score([d.nombre_archivo, d.descripcion], item_def.keywords_deteccion))
                    for d in secop_docs_contrato
                ]
                scored = [(d, s) for d, s in scored if s > 0]
                scored.sort(key=lambda x: x[1], reverse=True)
                candidatos_secop_src = [(d.id, d, s) for d, s in scored[:TOP_N_CANDIDATES]]

        df = fila.documento_fuente
        sd = fila.secop_documento

        # Pick the document pool for this row's tier (contract-level shared vs cuenta-level).
        _es_contrato = fila.requisito_codigo is not None and es_nivel_contrato(fila.requisito_codigo)
        docs_by_categoria = cat_contrato if _es_contrato else cat_cuenta
        docs_by_tipo = tipo_contrato if _es_contrato else tipo_cuenta

        # Build candidate list: categoria-matched first (more reliable), then tipo-matched.
        seen_cand_ids: set = {fila.documento_fuente_id} if fila.documento_fuente_id else set()
        candidatos_df: list[DocumentoFuente] = []
        # 1. Category-matched candidates (standard requisitos only)
        if req_cat_val:
            for d in docs_by_categoria.get(req_cat_val, []):
                if d.id not in seen_cand_ids:
                    seen_cand_ids.add(d.id)
                    candidatos_df.append(d)
        # 2. Tipo-matched candidates not already included
        if tipo_req:
            for d in docs_by_tipo.get(tipo_req, []):
                if d.id not in seen_cand_ids:
                    seen_cand_ids.add(d.id)
                    candidatos_df.append(d)
        # 3. Keyword-matched uploaded docs for custom requisitos (no categoria/tipo axis):
        #    score the document name + extracted text against the requisito's keywords.
        if fila.requisito_cuenta_id is not None:
            item_def = custom_by_id.get(fila.requisito_cuenta_id)
            if item_def is not None and item_def.keywords_deteccion:
                kw_scored = [
                    (d, _keyword_score([d.nombre, d.texto_extraido], item_def.keywords_deteccion)) for d in docs_cuenta
                ]
                kw_scored = [(d, s) for d, s in kw_scored if s > 0]
                kw_scored.sort(key=lambda x: x[1], reverse=True)
                for d, _s in kw_scored[:TOP_N_CANDIDATES]:
                    if d.id not in seen_cand_ids:
                        seen_cand_ids.add(d.id)
                        candidatos_df.append(d)

        items.append(
            {
                "requisito": requisito_block,
                "estado": fila.estado,
                "documento_fuente": _ref_documento_fuente(df) if df is not None else None,
                "secop_documento": _ref_secop_documento(sd) if sd is not None else None,
                "documentos_fuente": _todos_los_documentos_fuente(fila),
                "secop_documentos": _todos_los_secop_documentos(fila),
                "confianza_deteccion": fila.confianza_deteccion,
                "observaciones": fila.observaciones,
                "candidatos_secop": [
                    {
                        "secop_documento_id": sid,
                        "nombre_archivo": sdoc.nombre_archivo if sdoc else None,
                        "descripcion": sdoc.descripcion if sdoc else None,
                        "score": score,
                        "url_descarga": sdoc.url_descarga if sdoc else None,
                    }
                    for sid, sdoc, score in candidatos_secop_src
                ],
                "candidatos_documentos_fuente": [
                    {
                        "id": d.id,
                        "nombre": d.nombre,
                        "tipo": d.tipo.value if hasattr(d.tipo, "value") else str(d.tipo),
                        "categoria": d.categoria.value if d.categoria else None,
                        "categoria_confianza": d.categoria_confianza,
                        "categoria_override": d.categoria_override,
                    }
                    for d in candidatos_df
                ],
                "updated_at": fila.updated_at,
            }
        )

    resumen = computar_resumen(filas, catalogo, custom_by_id)
    arbol = await listar_arbol_evidencias(db, cuenta)

    return {
        "cuenta_cobro_id": cuenta.id,
        "requisitos_definidos": cuenta.requisitos_modo is not None,
        "items": items,
        "resumen": resumen,
        "arbol_evidencias": arbol,
    }
