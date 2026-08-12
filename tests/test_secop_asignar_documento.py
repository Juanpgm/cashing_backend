"""POST /api/v1/contratos/{id}/secop-documentos/{doc_id}/asignar — endpoint contract.

Covers slice B1 (migración 037): idempotent SECOP-document-to-slot assignment at
contract level, and the CDP enum regression (`TipoDocumentoFuente` previously had
no `cdp` member even though `checklist_service._CATALOGO_SEED` seeded it).
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.secop import SecopDocumento
from app.services import checklist_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-ASIGNAR-API-001",
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


def _secop_doc(numero_contrato: str, nombre: str = "documento.pdf", **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=numero_contrato,
        nombre_archivo=nombre,
        url_descarga=f"https://s/{uuid.uuid4().hex}.pdf",
        datos_raw={},
        **kwargs,
    )


@pytest.fixture
def descargas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    payloads: dict[str, bytes] = {}

    async def _fake(url: str, timeout: float = checklist_service._SNIFF_HTTP_TIMEOUT) -> bytes:
        if url not in payloads:
            raise ValueError(f"403 simulado para {url}")
        return payloads[url]

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake)
    return payloads


async def _documentos_fuente(
    db: AsyncSession, contrato_id: uuid.UUID, tipo: TipoDocumentoFuente
) -> list[DocumentoFuente]:
    res = await db.execute(
        select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato_id, DocumentoFuente.tipo == tipo)
    )
    return list(res.scalars().all())


@pytest.mark.asyncio
async def test_pertenencia_rechazada_404(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc("CTR-OTRO-CONTRATO")
    descargas[doc.url_descarga] = b"contenido"
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/secop-documentos/{doc.id}/asignar",
        json={"tipo": "rpc"},
        headers=test_user["headers"],
    )

    assert resp.status_code == 404
    assert await _documentos_fuente(db, contrato.id, TipoDocumentoFuente.RPC) == []


@pytest.mark.asyncio
async def test_asignacion_valida_crea_documento_fuente(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(contrato.numero_contrato)
    descargas[doc.url_descarga] = b"contenido rpc"
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/secop-documentos/{doc.id}/asignar",
        json={"tipo": "rpc"},
        headers=test_user["headers"],
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["tipo"] == "rpc"
    assert data["contrato_id"] == str(contrato.id)
    assert data["ya_existia"] is False

    creados = await _documentos_fuente(db, contrato.id, TipoDocumentoFuente.RPC)
    assert len(creados) == 1
    assert creados[0].cuenta_cobro_id is None


@pytest.mark.asyncio
async def test_repetir_asignacion_es_idempotente(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(contrato.numero_contrato)
    descargas[doc.url_descarga] = b"contenido cedula"
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    payload = {"tipo": "cedula"}
    first = await client.post(
        f"/api/v1/contratos/{contrato.id}/secop-documentos/{doc.id}/asignar",
        json=payload,
        headers=test_user["headers"],
    )
    assert first.status_code == 200
    assert first.json()["ya_existia"] is False

    second = await client.post(
        f"/api/v1/contratos/{contrato.id}/secop-documentos/{doc.id}/asignar",
        json=payload,
        headers=test_user["headers"],
    )
    assert second.status_code == 200
    assert second.json()["ya_existia"] is True
    assert second.json()["id"] == first.json()["id"]

    creados = await _documentos_fuente(db, contrato.id, TipoDocumentoFuente.CEDULA)
    assert len(creados) == 1


@pytest.mark.asyncio
async def test_tipo_invalido_422(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    doc = _secop_doc(contrato.numero_contrato)
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/secop-documentos/{doc.id}/asignar",
        json={"tipo": "instrucciones"},
        headers=test_user["headers"],
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_tipo_cdp_es_valido_regresion_enum(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, descargas: dict[str, Any]
) -> None:
    """Regression: TipoDocumentoFuente previously had no `cdp` member (migración 037)."""
    doc = _secop_doc(contrato.numero_contrato)
    descargas[doc.url_descarga] = b"contenido cdp"
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/secop-documentos/{doc.id}/asignar",
        json={"tipo": "cdp"},
        headers=test_user["headers"],
    )

    assert resp.status_code == 200
    assert resp.json()["tipo"] == "cdp"
