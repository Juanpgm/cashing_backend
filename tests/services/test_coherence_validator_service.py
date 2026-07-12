"""Tests for app.services.coherence_validator_service — pre-radicación rule engine.

Covers the R1-R6 rule catalog (see openspec/changes/billing-resilience-templates/
specs/radicacion-coherence-validator/spec.md). R6 (stale clause after Adición) is
built in this slice against a stubbed/empty `ValidationContext.adiciones` — the real
`adiciones_contrato` table lands in slice #4, which populates the context and adds
regression coverage against real data (Reconciliation Note 4 in tasks.md).

Slice #3 (migration 025) rewires R1 to read the persisted
`CuentaCobro.numero_cuota`/`posicion` fields instead of deriving them at read time
(task 3.12b) — `_make_cuenta` below now sets both explicitly, mirroring what
`cuenta_cobro_service.crear_cuenta_cobro` would assign for a real, sequentially
created cuota of the same contrato.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.core.exceptions import COHERENCE_CHECK_FAILED, ValidationError, domain_to_http
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import coherence_validator_service as cvs
from sqlalchemy.ext.asyncio import AsyncSession

# Note: `asyncio_mode = "auto"` (pyproject.toml) — async tests need no marker. This
# module deliberately mixes sync (pure dataclass/rule) and async (DB-backed) tests,
# so no module-level `pytestmark` is set (it would misfire warnings on the sync ones).


# ── 1.1/1.2 — error code wiring ─────────────────────────────────────────────


def test_coherence_check_failed_code_maps_to_http_422() -> None:
    exc = ValidationError("hallazgos duros encontrados", code=COHERENCE_CHECK_FAILED)
    http_exc = domain_to_http(exc)
    assert http_exc.status_code == 422
    assert exc.code == "COHERENCE_CHECK_FAILED"


# ── 1.3/1.4 — domain types ───────────────────────────────────────────────────


def test_severity_has_hard_and_soft() -> None:
    assert cvs.Severity.HARD == "hard"
    assert cvs.Severity.SOFT == "soft"


def test_finding_shape() -> None:
    finding = cvs.Finding(
        rule_id="R1",
        severity=cvs.Severity.HARD,
        codigo="STALE_CUOTA_NUMERO",
        mensaje="mensaje de prueba",
        contexto={"foo": "bar"},
    )
    assert finding.rule_id == "R1"
    assert finding.severity == cvs.Severity.HARD
    assert finding.codigo == "STALE_CUOTA_NUMERO"
    assert finding.contexto == {"foo": "bar"}


def test_validation_context_shape() -> None:
    ctx = cvs.ValidationContext(
        cuenta=None,  # type: ignore[arg-type]
        prior_cuenta=None,
        contrato=None,  # type: ignore[arg-type]
        obligaciones=[],
        actividades=[],
        documentos=[],
        documentos_prior=[],
        numero_cuota_derivado=1,
        adiciones=[],
    )
    assert ctx.numero_cuota_derivado == 1
    assert ctx.adiciones == []


def test_rules_registry_has_six_rules() -> None:
    assert len(cvs.RULES) == 6


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-COHER-001",
        objeto="Servicios profesionales para pruebas de coherencia",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _make_cuenta(
    db: AsyncSession,
    contrato: Contrato,
    mes: int,
    anio: int = 2024,
    valor: int = 1_000_000,
    numero_cuota: int = 1,
    posicion: PosicionCuota = PosicionCuota.PRIMERA,
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=valor,
        numero_cuota=numero_cuota,
        posicion=posicion,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _make_obligacion(db: AsyncSession, contrato: Contrato, descripcion: str, orden: int = 0) -> Obligacion:
    ob = Obligacion(contrato_id=contrato.id, descripcion=descripcion, tipo=TipoObligacion.GENERAL, orden=orden)
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return ob


async def _make_documento(
    db: AsyncSession,
    contrato: Contrato,
    cuenta: CuentaCobro,
    tipo: TipoDocumentoFuente,
    nombre: str,
    texto_extraido: str | None = None,
) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=contrato.usuario_id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        storage_key=f"fake/{nombre}",
        nombre=nombre,
        tipo=tipo,
        texto_extraido=texto_extraido,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


# ── R1: stale_cuota_numero ───────────────────────────────────────────────────


async def test_r1_no_finding_when_no_cuota_mention(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=3)
    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.stale_cuota_numero(ctx)
    assert findings == []


async def test_r1_hard_finding_on_stale_cuota_numero(db: AsyncSession, contrato: Contrato) -> None:
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(
        db, contrato, mes=2, numero_cuota=2, posicion=PosicionCuota.RECURRENTE
    )  # numero_cuota_derivado should be 2
    await _make_documento(
        db,
        contrato,
        cuenta2,
        TipoDocumentoFuente.INFORME_ACTIVIDADES,
        "informe.docx",
        texto_extraido="Informe correspondiente a la Cuota Número 1 del contrato.",
    )

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta2.id)
    assert ctx.numero_cuota_derivado == 2
    findings = cvs.stale_cuota_numero(ctx)
    assert len(findings) == 1
    assert findings[0].severity == cvs.Severity.HARD
    assert findings[0].rule_id == "R1"


async def test_r1_no_finding_when_cuota_numero_matches(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _make_documento(
        db,
        contrato,
        cuenta,
        TipoDocumentoFuente.INFORME_ACTIVIDADES,
        "informe.docx",
        texto_extraido="Informe correspondiente a la Cuota Número 1 del contrato.",
    )
    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.stale_cuota_numero(ctx)
    assert findings == []


async def test_r1_uses_stored_numero_cuota_not_chronological_derivation(db: AsyncSession, contrato: Contrato) -> None:
    """Task 3.12b: `numero_cuota_derivado` now comes from `CuentaCobro.numero_cuota`
    (migration 025), not from re-deriving it via chronological (anio, mes) order.
    Proven by a cuenta whose stored `numero_cuota` deliberately DIVERGES from what
    the old chronological-order heuristic would have produced — if the rewiring
    regressed to the old derivation, this would read 1 (chronologically first)
    instead of the stored 5."""
    cuenta = await _make_cuenta(db, contrato, mes=1, numero_cuota=5, posicion=PosicionCuota.RECURRENTE)
    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    assert ctx.numero_cuota_derivado == 5


async def test_r1_falls_back_to_derivation_when_numero_cuota_unset(db: AsyncSession, contrato: Contrato) -> None:
    """Defensive fallback (task 3.12b): a legacy cuota somehow missing the persisted
    `numero_cuota` field still gets a usable value via the old chronological-order
    derivation, instead of crashing the validator."""
    cuenta = await _make_cuenta(db, contrato, mes=1, numero_cuota=None, posicion=PosicionCuota.PRIMERA)  # type: ignore[arg-type]
    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    assert ctx.numero_cuota_derivado == 1


# ── R2: copied_accumulated_value ─────────────────────────────────────────────


async def test_r2_no_finding_without_prior_cuenta(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.copied_accumulated_value(ctx)
    assert findings == []


async def test_r2_hard_finding_on_copied_seguridad_social_block(db: AsyncSession, contrato: Contrato) -> None:
    cuenta1 = await _make_cuenta(db, contrato, mes=1, valor=1_000_000)
    cuenta2 = await _make_cuenta(
        db, contrato, mes=2, valor=1_000_000, numero_cuota=2, posicion=PosicionCuota.RECURRENTE
    )
    texto = "Planilla 123456789 pagada por el contratista."
    await _make_documento(db, contrato, cuenta1, TipoDocumentoFuente.SEGURIDAD_SOCIAL, "ss1.pdf", texto_extraido=texto)
    await _make_documento(db, contrato, cuenta2, TipoDocumentoFuente.SEGURIDAD_SOCIAL, "ss2.pdf", texto_extraido=texto)

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta2.id)
    findings = cvs.copied_accumulated_value(ctx)
    assert len(findings) == 1
    assert findings[0].severity == cvs.Severity.HARD
    assert findings[0].rule_id == "R2"


async def test_r2_no_finding_when_value_changed(db: AsyncSession, contrato: Contrato) -> None:
    cuenta1 = await _make_cuenta(db, contrato, mes=1, valor=1_000_000)
    cuenta2 = await _make_cuenta(
        db, contrato, mes=2, valor=1_200_000, numero_cuota=2, posicion=PosicionCuota.RECURRENTE
    )
    texto = "Planilla 123456789 pagada por el contratista."
    await _make_documento(db, contrato, cuenta1, TipoDocumentoFuente.SEGURIDAD_SOCIAL, "ss1.pdf", texto_extraido=texto)
    await _make_documento(db, contrato, cuenta2, TipoDocumentoFuente.SEGURIDAD_SOCIAL, "ss2.pdf", texto_extraido=texto)

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta2.id)
    findings = cvs.copied_accumulated_value(ctx)
    assert findings == []


# ── R3: pila_match ────────────────────────────────────────────────────────────


async def test_r3_hard_finding_on_pila_mismatch(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _make_documento(
        db,
        contrato,
        cuenta,
        TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        "planilla.pdf",
        texto_extraido="Planilla número 111222333 correspondiente al período.",
    )
    await _make_documento(
        db,
        contrato,
        cuenta,
        TipoDocumentoFuente.COMPROBANTE_PAGO_SS,
        "comprobante.pdf",
        texto_extraido="Comprobante de pago de la planilla 999888777.",
    )

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.pila_match(ctx)
    assert len(findings) == 1
    assert findings[0].severity == cvs.Severity.HARD
    assert findings[0].rule_id == "R3"


async def test_r3_no_finding_when_planilla_numbers_match(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _make_documento(
        db,
        contrato,
        cuenta,
        TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        "planilla.pdf",
        texto_extraido="Planilla número 111222333 correspondiente al período.",
    )
    await _make_documento(
        db,
        contrato,
        cuenta,
        TipoDocumentoFuente.COMPROBANTE_PAGO_SS,
        "comprobante.pdf",
        texto_extraido="Comprobante de pago de la planilla 111222333.",
    )

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.pila_match(ctx)
    assert findings == []


# ── R4: stale_month_in_filename (SOFT) ───────────────────────────────────────


async def test_r4_soft_finding_on_stale_month_in_filename(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=3)  # marzo
    await _make_documento(db, contrato, cuenta, TipoDocumentoFuente.INFORME_ACTIVIDADES, "informe-enero-2024.docx")

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.stale_month_in_filename(ctx)
    assert len(findings) == 1
    assert findings[0].severity == cvs.Severity.SOFT
    assert findings[0].rule_id == "R4"


async def test_r4_no_finding_when_filename_month_matches(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=3)  # marzo
    await _make_documento(db, contrato, cuenta, TipoDocumentoFuente.INFORME_ACTIVIDADES, "informe-marzo-2024.docx")

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.stale_month_in_filename(ctx)
    assert findings == []


# ── R5: obligacion_text_mapping ───────────────────────────────────────────────


async def test_r5_no_finding_after_8_to_7_count_shift(db: AsyncSession, contrato: Contrato) -> None:
    """Distinct obligación text survives a count shift without a false HARD finding."""
    descripciones = [
        "Asistir a reuniones de seguimiento del supervisor",
        "Elaborar informes mensuales de actividades ejecutadas",
        "Apoyar la gestión documental del área de sistemas",
        "Realizar mantenimiento preventivo de equipos de cómputo",
        "Brindar soporte técnico a usuarios internos",
        "Actualizar el inventario de activos tecnológicos",
        "Capacitar al personal en el uso de nuevas herramientas",
    ]
    for i, descripcion in enumerate(descripciones):
        await _make_obligacion(db, contrato, descripcion, orden=i)
    cuenta = await _make_cuenta(db, contrato, mes=1)

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.obligacion_text_mapping(ctx)
    assert findings == []


async def test_r5_hard_finding_on_ambiguous_obligacion_text(db: AsyncSession, contrato: Contrato) -> None:
    await _make_obligacion(db, contrato, "Asistir a reuniones de seguimiento del contrato", orden=0)
    await _make_obligacion(db, contrato, "Asistir a reuniones de seguimiento del contrato ", orden=1)
    cuenta = await _make_cuenta(db, contrato, mes=1)

    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    findings = cvs.obligacion_text_mapping(ctx)
    assert len(findings) == 1
    assert findings[0].severity == cvs.Severity.HARD
    assert findings[0].rule_id == "R5"


# ── R6: stale_clause_after_adicion (stub — real table lands slice #4) ───────


async def test_r6_no_finding_with_empty_stubbed_adiciones(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    assert ctx.adiciones == []
    findings = cvs.stale_clause_after_adicion(ctx)
    assert findings == []


async def test_r6_hard_finding_when_stale_rpc_present_in_stubbed_context(db: AsyncSession, contrato: Contrato) -> None:
    """Exercises the rule's interface against a manually-populated stub context —
    the real `adiciones_contrato` wiring lands in slice #4 (Reconciliation Note 4)."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _make_documento(
        db,
        contrato,
        cuenta,
        TipoDocumentoFuente.RPC,
        "rpc.pdf",
        texto_extraido="Registro Presupuestal de Compromiso RPC-100 del contrato.",
    )
    ctx = await cvs._build_context(db, contrato.usuario_id, cuenta.id)
    ctx_con_adicion = cvs.ValidationContext(
        cuenta=ctx.cuenta,
        prior_cuenta=ctx.prior_cuenta,
        contrato=ctx.contrato,
        obligaciones=ctx.obligaciones,
        actividades=ctx.actividades,
        documentos=ctx.documentos,
        documentos_prior=ctx.documentos_prior,
        numero_cuota_derivado=ctx.numero_cuota_derivado,
        adiciones=[{"rpc_anterior": "RPC-100", "rpc_nuevo": "RPC-200"}],
    )
    findings = cvs.stale_clause_after_adicion(ctx_con_adicion)
    assert len(findings) == 1
    assert findings[0].severity == cvs.Severity.HARD
    assert findings[0].rule_id == "R6"


# ── validar_coherencia entrypoint ─────────────────────────────────────────────


async def test_validar_coherencia_runs_full_registry(db: AsyncSession, contrato: Contrato) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    findings = await cvs.validar_coherencia(db, contrato.usuario_id, cuenta.id)
    assert findings == []


async def test_validar_coherencia_aggregates_hard_and_soft(db: AsyncSession, contrato: Contrato) -> None:
    await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2, numero_cuota=2, posicion=PosicionCuota.RECURRENTE)
    await _make_documento(db, contrato, cuenta2, TipoDocumentoFuente.INFORME_ACTIVIDADES, "informe-enero-2024.docx")

    findings = await cvs.validar_coherencia(db, contrato.usuario_id, cuenta2.id)
    assert any(f.severity == cvs.Severity.SOFT for f in findings)


async def test_validar_coherencia_raises_forbidden_for_other_user(db: AsyncSession, contrato: Contrato) -> None:
    import uuid

    from app.core.exceptions import ForbiddenError

    cuenta = await _make_cuenta(db, contrato, mes=1)
    with pytest.raises(ForbiddenError):
        await cvs.validar_coherencia(db, uuid.uuid4(), cuenta.id)
