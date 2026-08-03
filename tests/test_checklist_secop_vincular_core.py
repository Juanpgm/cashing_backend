"""Raising core for manual Vincular SECOP doc -> obligaciones extraction.

`checklist_service.extraer_obligaciones_desde_secop_doc` is the RAISING sibling of
the swallowing `_auto_extraer_obligaciones_contrato` (task 3.9): same ensure-text +
extract pipeline, but propagates failures instead of degrading -- required to back
a user-facing endpoint (Vincular) that must report a 4xx instead of a misleading
200 (design-technical.md, decision D2).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from typing import Any

import pytest
from app.core.exceptions import ValidationError
from app.models.contrato import Contrato
from app.models.secop import SecopDocumento
from app.schemas.agent import ObligacionExtraida
from app.services import checklist_service
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

TEXTO_CONTRATO = (
    "CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES No. 99\n"
    "Entre la entidad y el contratista se celebra el presente contrato, cuyo clausulado se detalla."
)


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-VINC-CORE-001",
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


@pytest.fixture
def extraccion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Spy on `document_service.extraer_obligaciones_texto`."""
    from app.services import document_service

    llamadas: list[tuple[str, uuid.UUID]] = []
    resultado = ([ObligacionExtraida(descripcion="Obligación vinculada", tipo="general", orden=1)], [])

    async def _fake(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        llamadas.append((texto, contrato_id))
        return resultado

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _fake)
    return {"llamadas": llamadas, "resultado": resultado}


async def test_happy_path_retorna_obligaciones_y_avisos(
    db: AsyncSession, contrato: Contrato, extraccion: dict[str, Any]
) -> None:
    """Memoized `texto_estado='ok'` doc: no download, delegates straight to
    `document_service.extraer_obligaciones_texto` and returns its result."""
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="ok", texto_extraido=TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()

    obligaciones, avisos = await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc)

    assert (obligaciones, avisos) == extraccion["resultado"]
    assert extraccion["llamadas"] == [(TEXTO_CONTRATO, contrato.id)]


async def test_sin_texto_utilizable_lanza_excepcion_de_dominio(
    db: AsyncSession, contrato: Contrato, extraccion: dict[str, Any]
) -> None:
    """A doc whose text could not be recovered (memoized `sin_texto`/`error`,
    or missing content) RAISES instead of silently degrading."""
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="sin_texto", texto_extraido=None)
    db.add(doc)
    await db.commit()

    with pytest.raises(ValidationError):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc)

    assert extraccion["llamadas"] == []


async def test_timeout_de_extraccion_lanza_timeouterror(
    db: AsyncSession, contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow `extraer_obligaciones_texto` past the deadline raises `TimeoutError`
    -- callers (the manual Vincular service) decide how to map it to a 4xx; the
    scan's degraded wrapper keeps its own specific timeout log/degrade branch."""
    from app.services import document_service

    async def _lento(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        await asyncio.sleep(0.15)
        return [], []

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _lento)
    monkeypatch.setattr(checklist_service, "_AUTO_OBLIGACIONES_TIMEOUT_SEGUNDOS", 0.01)
    doc = _secop_doc(contrato, "contrato.docx", texto_estado="ok", texto_extraido=TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()

    with pytest.raises(TimeoutError):
        await checklist_service.extraer_obligaciones_desde_secop_doc(db, contrato, doc)
