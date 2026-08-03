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

import pytest
from app.models.contrato import Contrato
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
