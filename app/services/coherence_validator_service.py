"""Coherence validator — pre-radicación read-only rule engine.

Runs the R1-R6 rule catalog (see
`openspec/changes/billing-resilience-templates/specs/radicacion-coherence-validator/spec.md`)
over a cuenta de cobro before it is allowed to radicar. Loads a `ValidationContext`
once, then runs every registered `CoherenceRule` over it. The caller
(`cuenta_cobro_service.radicar_cuenta`) raises `COHERENCE_CHECK_FAILED` on any HARD
finding; SOFT findings never block.

R6 (`stale_clause_after_adicion`) is a deliberate STUB in this slice: the real
`adiciones_contrato` table lands in slice #4 (migration 026), so
`ValidationContext.adiciones` is always an empty list here and the rule is a no-op.
Slice #4 populates `adiciones` with real events without changing this rule's logic
(Reconciliation Note 4, tasks.md).

Deviation from the literal spec text for R1: `CuentaCobro.numero_cuota`/`posicion`
do not exist yet (they land in slice #3, migration 025). This module derives the
expected cuota number from chronological (anio, mes) ordering among sibling cuentas
of the same contrato — the same technique `checklist_service._is_first_cuenta`
already uses and that slice #3 formalizes into a persisted field.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core import text_match
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion

_MESES = [
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
]

_SEG_SOCIAL_TIPOS = {TipoDocumentoFuente.SEGURIDAD_SOCIAL, TipoDocumentoFuente.COMPROBANTE_PAGO_SS}
_CUOTA_PATTERN = re.compile(r"cuota[^\d]{0,20}(\d{1,3})", re.IGNORECASE)
_PLANILLA_PATTERN = re.compile(r"planilla[^\d]{0,20}(\d{3,})", re.IGNORECASE)
_AMBIGUOUS_THRESHOLD = Decimal("0.900")


class Severity(StrEnum):
    HARD = "hard"
    SOFT = "soft"


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: Severity
    codigo: str
    mensaje: str
    contexto: dict[str, Any]


@dataclass(frozen=True)
class ValidationContext:
    cuenta: CuentaCobro
    prior_cuenta: CuentaCobro | None
    contrato: Contrato
    obligaciones: list[Obligacion]
    actividades: list[Actividad]
    documentos: list[DocumentoFuente]
    documentos_prior: list[DocumentoFuente]
    numero_cuota_derivado: int
    # Stubbed/empty until slice #4 wires the real `adiciones_contrato` table
    # (Reconciliation Note 4). Each item is expected to carry keys such as
    # `rpc_anterior`/`rpc_nuevo`/`cdp_anterior`/`cdp_nuevo` once populated for real.
    adiciones: list[dict[str, Any]] = field(default_factory=list)


CoherenceRule = Callable[[ValidationContext], list[Finding]]
RULES: list[CoherenceRule] = []


def _register(rule: CoherenceRule) -> CoherenceRule:
    RULES.append(rule)
    return rule


# ── R1 — stale cuota número ──────────────────────────────────────────────────


@_register
def stale_cuota_numero(ctx: ValidationContext) -> list[Finding]:
    """HARD: an explicit "Cuota N" mention in the cuenta's own text diverges from
    the derived cuota number (see module docstring for the interim derivation)."""
    textos: list[str] = []
    for act in ctx.actividades:
        if act.descripcion:
            textos.append(act.descripcion)
        if act.justificacion:
            textos.append(act.justificacion)
    for doc in ctx.documentos:
        if doc.nombre:
            textos.append(doc.nombre)
        if doc.texto_extraido:
            textos.append(doc.texto_extraido)

    for texto in textos:
        match = _CUOTA_PATTERN.search(texto)
        if match is None:
            continue
        numero_mencionado = int(match.group(1))
        if numero_mencionado != ctx.numero_cuota_derivado:
            return [
                Finding(
                    rule_id="R1",
                    severity=Severity.HARD,
                    codigo="STALE_CUOTA_NUMERO",
                    mensaje=(
                        f"El texto menciona 'Cuota {numero_mencionado}' pero esta cuenta corresponde "
                        f"a la cuota número {ctx.numero_cuota_derivado} del contrato."
                    ),
                    contexto={
                        "numero_mencionado": numero_mencionado,
                        "numero_cuota_derivado": ctx.numero_cuota_derivado,
                    },
                )
            ]
    return []


# ── R2 — copied accumulated value / seg-social block ─────────────────────────


@_register
def copied_accumulated_value(ctx: ValidationContext) -> list[Finding]:
    """HARD: `valor` AND a seg-social document's extracted text are both byte-identical
    to the prior cuota — unless the prior cuota simply does not exist yet."""
    if ctx.prior_cuenta is None:
        return []

    valor_actual = Decimal(str(ctx.cuenta.valor))
    valor_prior = Decimal(str(ctx.prior_cuenta.valor))
    if valor_actual != valor_prior:
        return []

    docs_actual = {d.tipo: d.texto_extraido for d in ctx.documentos if d.tipo in _SEG_SOCIAL_TIPOS and d.texto_extraido}
    docs_prior = {
        d.tipo: d.texto_extraido for d in ctx.documentos_prior if d.tipo in _SEG_SOCIAL_TIPOS and d.texto_extraido
    }

    findings: list[Finding] = []
    for tipo, texto in docs_actual.items():
        if docs_prior.get(tipo) == texto:
            findings.append(
                Finding(
                    rule_id="R2",
                    severity=Severity.HARD,
                    codigo="COPIED_ACCUMULATED_VALUE",
                    mensaje=(
                        f"El valor y el documento '{tipo.value}' son idénticos a los de la cuota "
                        "anterior — revise si realmente no hubo actividad nueva."
                    ),
                    contexto={"tipo_documento": tipo.value, "valor": str(valor_actual)},
                )
            )
    return findings


# ── R3 — PILA planilla match within the same cuota ───────────────────────────


@_register
def pila_match(ctx: ValidationContext) -> list[Finding]:
    """HARD: the PILA planilla number diverges between the planilla and the
    comprobante de pago documents of the SAME cuenta."""
    planilla_por_tipo: dict[TipoDocumentoFuente, str] = {}
    for doc in ctx.documentos:
        if doc.tipo not in _SEG_SOCIAL_TIPOS or not doc.texto_extraido:
            continue
        match = _PLANILLA_PATTERN.search(doc.texto_extraido)
        if match:
            planilla_por_tipo[doc.tipo] = match.group(1)

    numeros = set(planilla_por_tipo.values())
    if len(numeros) <= 1:
        return []

    return [
        Finding(
            rule_id="R3",
            severity=Severity.HARD,
            codigo="PILA_MISMATCH",
            mensaje=(
                "El número de planilla PILA no coincide entre los documentos de seguridad "
                "social de esta cuenta."
            ),
            contexto={"planillas": {tipo.value: numero for tipo, numero in planilla_por_tipo.items()}},
        )
    ]


# ── R4 — stale month name in filename (SOFT) ─────────────────────────────────


@_register
def stale_month_in_filename(ctx: ValidationContext) -> list[Finding]:
    """SOFT: a document filename mentions a month different from the cuenta's period."""
    mes_actual = _MESES[ctx.cuenta.mes - 1]
    findings: list[Finding] = []
    for doc in ctx.documentos:
        nombre_norm = text_match.normalize(doc.nombre)
        for mes in _MESES:
            if mes != mes_actual and mes in nombre_norm:
                findings.append(
                    Finding(
                        rule_id="R4",
                        severity=Severity.SOFT,
                        codigo="STALE_MONTH_IN_FILENAME",
                        mensaje=(
                            f"El archivo '{doc.nombre}' menciona '{mes}' pero esta cuenta "
                            f"corresponde a {mes_actual}."
                        ),
                        contexto={
                            "documento_id": str(doc.id),
                            "mes_archivo": mes,
                            "mes_cuenta": mes_actual,
                        },
                    )
                )
                break
    return findings


# ── R5 — obligación text mapping (map by TEXT, not letter) ──────────────────


@_register
def obligacion_text_mapping(ctx: ValidationContext) -> list[Finding]:
    """HARD: two obligaciones of the contract are textually indistinguishable, so
    any letter/position-based mapping (or a text match) cannot resolve uniquely.

    Tolerates an obligación-count shift (e.g. 8→7) by design: comparison is purely
    pairwise over the CURRENT obligación list, never against a stored letter/index.
    """
    findings: list[Finding] = []
    obligaciones = ctx.obligaciones
    for i, a in enumerate(obligaciones):
        for b in obligaciones[i + 1 :]:
            score = text_match.similar(a.descripcion, b.descripcion)
            if score >= _AMBIGUOUS_THRESHOLD:
                findings.append(
                    Finding(
                        rule_id="R5",
                        severity=Severity.HARD,
                        codigo="OBLIGACION_TEXT_AMBIGUOUS",
                        mensaje=(
                            f"Las obligaciones '{a.descripcion}' y '{b.descripcion}' tienen texto "
                            "casi idéntico — la asignación de evidencia por texto es ambigua."
                        ),
                        contexto={
                            "obligacion_a": str(a.id),
                            "obligacion_b": str(b.id),
                            "similitud": str(score),
                        },
                    )
                )
    return findings


# ── R6 — stale clause after Adición (STUB — real table lands slice #4) ─────


@_register
def stale_clause_after_adicion(ctx: ValidationContext) -> list[Finding]:
    """HARD: after a recorded Adición event, the new RPC/CDP does not appear in the
    cuenta's text while the pre-Adición identifier still does.

    STUB in this slice: `ctx.adiciones` is always empty (see module docstring),
    so this rule is a no-op until slice #4 wires real Adición events in.
    """
    if not ctx.adiciones:
        return []

    textos = [doc.texto_extraido for doc in ctx.documentos if doc.texto_extraido]
    textos += [act.descripcion for act in ctx.actividades if act.descripcion]
    blob_norm = text_match.normalize(" ".join(textos))

    findings: list[Finding] = []
    for adicion in ctx.adiciones:
        for campo, clave_nueva, clave_anterior in (
            ("RPC", "rpc_nuevo", "rpc_anterior"),
            ("CDP", "cdp_nuevo", "cdp_anterior"),
        ):
            nuevo = adicion.get(clave_nueva)
            anterior = adicion.get(clave_anterior)
            if not nuevo or not anterior:
                continue
            anterior_norm = text_match.normalize(anterior)
            nuevo_norm = text_match.normalize(nuevo)
            if anterior_norm in blob_norm and nuevo_norm not in blob_norm:
                findings.append(
                    Finding(
                        rule_id="R6",
                        severity=Severity.HARD,
                        codigo="STALE_CLAUSE_AFTER_ADICION",
                        mensaje=(
                            f"El {campo} anterior ({anterior}) sigue apareciendo en el texto de la "
                            f"cuenta, pero el nuevo {campo} tras la Adición ({nuevo}) no."
                        ),
                        contexto={"campo": campo, "anterior": anterior, "nuevo": nuevo},
                    )
                )
    return findings


# ── Context loading + entrypoint ─────────────────────────────────────────────


async def _load_cuenta(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> CuentaCobro:
    """Load a CuentaCobro with its contrato, verifying ownership.

    Deliberately does NOT import `cuenta_cobro_service` (which imports THIS module to
    wire the gate) — mirrors the existing `informe_service._load_context` pattern of
    each service independently loading and verifying its own ownership context.

    Actividades and obligaciones are loaded via explicit separate queries
    (`_load_actividades`/`_load_obligaciones`) rather than the `lazy="selectin"`
    relationships on these models — those relationship collections can be cached
    stale in the session identity map when rows are inserted AFTER an earlier
    `refresh()`/query already marked the collection "loaded" (as empty) within the
    same session, which test fixtures (and some request flows) routinely do.
    """
    result = await db.execute(
        select(CuentaCobro)
        .options(selectinload(CuentaCobro.contrato))
        .where(CuentaCobro.id == cuenta_id, CuentaCobro.deleted_at.is_(None))
    )
    cuenta = result.scalar_one_or_none()
    if cuenta is None:
        raise NotFoundError("CuentaCobro", str(cuenta_id))
    if cuenta.contrato.usuario_id != usuario_id:
        raise ForbiddenError()
    return cuenta


async def _load_obligaciones(db: AsyncSession, contrato_id: uuid.UUID) -> list[Obligacion]:
    result = await db.execute(
        select(Obligacion)
        .where(Obligacion.contrato_id == contrato_id)
        .order_by(Obligacion.orden.asc(), Obligacion.created_at.asc())
    )
    return list(result.scalars().all())


async def _load_actividades(db: AsyncSession, cuenta_id: uuid.UUID) -> list[Actividad]:
    result = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))
    return list(result.scalars().all())


async def _derive_numero_cuota(db: AsyncSession, cuenta: CuentaCobro) -> int:
    """1-based ordinal position of `cuenta` among its contrato's cuentas, ordered
    chronologically by (anio, mes). Interim signal until slice #3 persists a real
    `numero_cuota` field (see module docstring)."""
    result = await db.execute(
        select(CuentaCobro.id)
        .where(CuentaCobro.contrato_id == cuenta.contrato_id, CuentaCobro.deleted_at.is_(None))
        .order_by(CuentaCobro.anio.asc(), CuentaCobro.mes.asc())
    )
    ids = [row[0] for row in result.all()]
    try:
        return ids.index(cuenta.id) + 1
    except ValueError:
        return 1


async def _load_prior_cuenta(db: AsyncSession, cuenta: CuentaCobro) -> CuentaCobro | None:
    """The chronologically immediately-preceding cuenta of the same contrato, if any."""
    result = await db.execute(
        select(CuentaCobro)
        .where(
            CuentaCobro.contrato_id == cuenta.contrato_id,
            CuentaCobro.deleted_at.is_(None),
            CuentaCobro.id != cuenta.id,
            or_(
                CuentaCobro.anio < cuenta.anio,
                and_(CuentaCobro.anio == cuenta.anio, CuentaCobro.mes < cuenta.mes),
            ),
        )
        .order_by(CuentaCobro.anio.desc(), CuentaCobro.mes.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _load_documentos(db: AsyncSession, cuenta_id: uuid.UUID) -> list[DocumentoFuente]:
    result = await db.execute(select(DocumentoFuente).where(DocumentoFuente.cuenta_cobro_id == cuenta_id))
    return list(result.scalars().all())


async def _build_context(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> ValidationContext:
    cuenta = await _load_cuenta(db, usuario_id, cuenta_id)
    contrato = cuenta.contrato
    obligaciones = await _load_obligaciones(db, contrato.id)
    actividades = await _load_actividades(db, cuenta.id)

    numero_cuota = await _derive_numero_cuota(db, cuenta)
    prior_cuenta = await _load_prior_cuenta(db, cuenta)
    documentos = await _load_documentos(db, cuenta.id)
    documentos_prior = await _load_documentos(db, prior_cuenta.id) if prior_cuenta is not None else []

    return ValidationContext(
        cuenta=cuenta,
        prior_cuenta=prior_cuenta,
        contrato=contrato,
        obligaciones=obligaciones,
        actividades=actividades,
        documentos=documentos,
        documentos_prior=documentos_prior,
        numero_cuota_derivado=numero_cuota,
        adiciones=[],
    )


async def validar_coherencia(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> list[Finding]:
    """Run the full R1-R6 rule catalog over a cuenta. Read-only — never raises on a
    HARD finding itself; the caller (`cuenta_cobro_service.radicar_cuenta`) decides
    whether/how to block."""
    ctx = await _build_context(db, usuario_id, cuenta_id)
    findings: list[Finding] = []
    for rule in RULES:
        findings.extend(rule(ctx))
    return findings
