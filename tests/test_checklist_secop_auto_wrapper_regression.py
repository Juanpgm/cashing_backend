"""Regression pin: `_auto_extraer_obligaciones_contrato` never breaks the scan.

Direct-call characterization test for the "never break the scan" contract
(design-technical.md R-A / D2): after the D2 refactor extracted a RAISING core
(`extraer_obligaciones_desde_secop_doc`), the scan's wrapper must still return
`None` and swallow every failure path -- it must never propagate an exception
to its caller (`detectar_desde_secop`).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
import structlog
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-VINC-WRAP-001",
        objeto="Servicios de extracción",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


def _secop_doc(contrato: Contrato, nombre: str, **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo=nombre,
        datos_raw={},
        **kwargs,
    )


async def test_swallow_en_texto_no_utilizable(db: AsyncSession, contrato: Contrato) -> None:
    """Core raises ValidationError (no usable text) -> wrapper returns None, no exception."""
    doc = _secop_doc(contrato, "sin_texto.docx", texto_estado="sin_texto", texto_extraido=None)
    db.add(doc)
    await db.commit()
    presupuesto = checklist_service._PresupuestoSniff()

    resultado = await checklist_service._auto_extraer_obligaciones_contrato(db, contrato, doc, presupuesto)

    assert resultado is None


async def test_swallow_en_excepcion_inesperada_de_extraccion(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected exception from the extraction pipeline is swallowed too."""
    from app.services import document_service

    async def _falla(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        raise RuntimeError("fallo simulado de extracción")

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _falla)
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="ok", texto_extraido="texto de prueba")
    db.add(doc)
    await db.commit()
    presupuesto = checklist_service._PresupuestoSniff()

    resultado = await checklist_service._auto_extraer_obligaciones_contrato(db, contrato, doc, presupuesto)

    assert resultado is None


async def test_skip_si_contrato_ya_tiene_obligaciones(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FIX-3(a): auto-extraction must NEVER append when the contrato already has
    obligaciones (idempotence guard) — regardless of which document/source they came
    from. It must not even attempt the (possibly foreign) extraction."""
    from sqlalchemy import select

    db.add(
        Obligacion(
            contrato_id=contrato.id,
            descripcion="Obligación ya existente",
            tipo=TipoObligacion.ESPECIFICA,
            orden=1,
        )
    )
    await db.commit()

    spy = AsyncMock(side_effect=AssertionError("extraction must not run when obligaciones already exist"))
    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", spy)

    doc = _secop_doc(contrato, "otro_documento.docx", texto_estado="ok", texto_extraido="texto de prueba")
    db.add(doc)
    await db.commit()
    presupuesto = checklist_service._PresupuestoSniff()

    await checklist_service._auto_extraer_obligaciones_contrato(db, contrato, doc, presupuesto)

    spy.assert_not_called()
    rows = (await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato.id))).scalars().all()
    assert len(rows) == 1  # unchanged


async def test_texto_con_numero_de_otro_contrato_bloquea_extraccion(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DAGMA incident guard: the doc's extracted TEXT mentions a DIFFERENT
    contract's number -> extraction must be skipped entirely (no obligaciones
    persisted), even though the doc's own metadata (numero_contrato) matches
    the target contrato -- the incident's leaked doc carried correct-looking
    metadata but foreign content."""
    spy = AsyncMock(side_effect=AssertionError("extraction must not run for a foreign contract's text"))
    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", spy)

    doc = _secop_doc(
        contrato,
        "contrato_ajeno.docx",
        texto_estado="ok",
        texto_extraido=(
            "CONTRATO DE PRESTACIÓN DE SERVICIOS No. CTR-OTRO-999\n"
            "Obligaciones del contratista: prestar el servicio contratado."
        ),
    )
    db.add(doc)
    await db.commit()
    presupuesto = checklist_service._PresupuestoSniff()

    with structlog.testing.capture_logs() as captured:
        resultado = await checklist_service._auto_extraer_obligaciones_contrato(db, contrato, doc, presupuesto)

    assert resultado is None
    spy.assert_not_called()
    rechazos = [e for e in captured if e.get("event") == "checklist_auto_extraccion_pertenencia_rechazada"]
    assert rechazos, "expected a structured pertenencia-rejection log"
    assert rechazos[0]["contrato_id"] == str(contrato.id)
    assert rechazos[0]["numero_contrato"] == contrato.numero_contrato


async def test_texto_con_numero_propio_en_variante_tolerante_permite_extraccion(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doc text mentions the TARGET contrato's own number, just written in a
    tolerant format variant (lowercase, dashes swapped for dots) -> extraction
    proceeds as before."""
    called = {"core": False}

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[Any], list[str]]:
        called["core"] = True
        return [], []

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)

    variante = contrato.numero_contrato.lower().replace("-", ".")
    doc = _secop_doc(
        contrato,
        "contrato_propio.docx",
        texto_estado="ok",
        texto_extraido=f"Contrato No. {variante}\nObligaciones del contratista.",
    )
    db.add(doc)
    await db.commit()
    presupuesto = checklist_service._PresupuestoSniff()

    await checklist_service._auto_extraer_obligaciones_contrato(db, contrato, doc, presupuesto)

    assert called["core"] is True


async def test_texto_sin_numero_detectable_permite_extraccion(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No contract-number mention at all in the text -> UNDETERMINABLE, so
    current behavior is preserved (extraction proceeds; do not over-block)."""
    called = {"core": False}

    async def _core_spy(*args: Any, **kwargs: Any) -> tuple[list[Any], list[str]]:
        called["core"] = True
        return [], []

    monkeypatch.setattr(checklist_service, "extraer_obligaciones_desde_secop_doc", _core_spy)

    doc = _secop_doc(
        contrato,
        "sin_numero.docx",
        texto_estado="ok",
        texto_extraido="Documento de soporte sin encabezado identificable.",
    )
    db.add(doc)
    await db.commit()
    presupuesto = checklist_service._PresupuestoSniff()

    await checklist_service._auto_extraer_obligaciones_contrato(db, contrato, doc, presupuesto)

    assert called["core"] is True
