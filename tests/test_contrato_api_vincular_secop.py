"""POST /api/v1/contratos/{id}/vincular-secop-documento — endpoint contract.

Covers spec.md "Vincular endpoint" requirement: mismatch rejection, clear 4xx on
download/extract failure (no misleading 200), happy path extraction, idempotency
(no duplicate Obligacion on repeat calls — dedup lives in
`document_service._persist_obligaciones`), and that a non-CONTRATO-classified
document is still allowed (manual override is the point).
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from typing import Any

import pytest
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion
from app.models.secop import SecopDocumento
from app.services import checklist_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

OBLIGACIONES_VERBATIM = [
    "Diseñar e implementar los módulos del sistema de información SISBEN IV "
    "según los requerimientos técnicos definidos por la Secretaría",
    "Realizar las pruebas unitarias y de integración de cada componente "
    "antes de su despliegue en el ambiente de producción",
    "Capacitar a los funcionarios de la Alcaldía de Cali en el uso del "
    "nuevo sistema mediante talleres presenciales mensuales",
    "Documentar los procedimientos técnicos y manuales de usuario en español, con ejemplos y capturas de pantalla",
    "Brindar soporte técnico de segundo nivel durante los noventa (90) días posteriores a la puesta en producción",
]

TEXTO_CONTRATO = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2024-0123

ENTRE LA ALCALDÍA DE SANTIAGO DE CALI Y EL CONTRATISTA, SE CELEBRA EL
PRESENTE CONTRATO, PREVIAS LAS SIGUIENTES CONSIDERACIONES:

CLÁUSULA PRIMERA — OBJETO: El contratista se obliga a prestar sus
servicios profesionales para el desarrollo del sistema SISBEN IV.

CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

1. {OBLIGACIONES_VERBATIM[0]}.
2. {OBLIGACIONES_VERBATIM[1]}.
3. {OBLIGACIONES_VERBATIM[2]}.
4. {OBLIGACIONES_VERBATIM[3]}.
5. {OBLIGACIONES_VERBATIM[4]}.

CLÁUSULA TERCERA — OBLIGACIONES GENERALES: El contratista deberá pagar
los aportes a seguridad social, guardar confidencialidad de la
información y asistir a las reuniones convocadas por el supervisor.

CLÁUSULA CUARTA — VALOR DEL CONTRATO: Treinta y seis millones de pesos.
""".strip()


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-VINC-API-001",
        objeto="Prestación de servicios de consultoría tecnológica",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Alcaldía de Santiago de Cali",
        dependencia="Dirección de Sistemas",
        supervisor_nombre="Pedro Supervisor",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


def _secop_doc(numero_contrato: str, nombre: str = "documento.docx", **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=numero_contrato,
        nombre_archivo=nombre,
        url_descarga=f"https://s/{uuid.uuid4().hex}.docx",
        datos_raw={},
        **kwargs,
    )


def _docx_bytes(texto: str) -> bytes:
    import docx

    d = docx.Document()
    for parrafo in texto.split("\n"):
        d.add_paragraph(parrafo)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def descargas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    payloads: dict[str, bytes] = {}

    async def _fake(url: str, timeout: float = checklist_service._SNIFF_HTTP_TIMEOUT) -> bytes:
        if url not in payloads:
            raise ValueError(f"403 simulado para {url}")
        return payloads[url]

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake)
    return payloads


async def _obligaciones(db: AsyncSession, contrato_id: uuid.UUID) -> list[Obligacion]:
    res = await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato_id))
    return list(res.scalars().all())


@pytest.mark.asyncio
async def test_mismatch_numero_contrato_4xx(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc("CTR-OTRO-CONTRATO")
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/vincular-secop-documento",
        json={"secop_documento_id": str(doc.id)},
        headers=test_user["headers"],
    )

    assert 400 <= resp.status_code < 500
    assert await _obligaciones(db, contrato.id) == []


@pytest.mark.asyncio
async def test_descarga_falla_4xx_con_mensaje_claro(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(contrato.numero_contrato)  # url not registered in `descargas` -> download raises
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/vincular-secop-documento",
        json={"secop_documento_id": str(doc.id)},
        headers=test_user["headers"],
    )

    assert 400 <= resp.status_code < 500
    assert resp.json()["detail"]
    assert await _obligaciones(db, contrato.id) == []


@pytest.mark.asyncio
async def test_happy_path_200_extrae_obligaciones(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(contrato.numero_contrato)
    descargas[doc.url_descarga] = _docx_bytes(TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/vincular-secop-documento",
        json={"secop_documento_id": str(doc.id)},
        headers=test_user["headers"],
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["contrato_id"] == str(contrato.id)
    assert data["total"] == 5
    assert len(data["obligaciones"]) == 5
    assert len(await _obligaciones(db, contrato.id)) == 5


@pytest.mark.asyncio
async def test_idempotencia_no_duplica_obligaciones(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(contrato.numero_contrato)
    descargas[doc.url_descarga] = _docx_bytes(TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    payload = {"secop_documento_id": str(doc.id)}
    first = await client.post(
        f"/api/v1/contratos/{contrato.id}/vincular-secop-documento", json=payload, headers=test_user["headers"]
    )
    assert first.status_code == 200
    assert len(await _obligaciones(db, contrato.id)) == 5

    second = await client.post(
        f"/api/v1/contratos/{contrato.id}/vincular-secop-documento", json=payload, headers=test_user["headers"]
    )
    assert second.status_code == 200
    assert len(await _obligaciones(db, contrato.id)) == 5  # unchanged, not 10


@pytest.mark.asyncio
async def test_documento_no_clasificado_como_contrato_igual_permite_vincular(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(contrato.numero_contrato, categoria=CategoriaDocumento.OTROS)
    descargas[doc.url_descarga] = _docx_bytes(TEXTO_CONTRATO)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/vincular-secop-documento",
        json={"secop_documento_id": str(doc.id)},
        headers=test_user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["total"] == 5
