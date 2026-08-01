"""Context-docs surface (clasificacion-documentos-secop, design D4).

`listar_documentos_contexto` returns the contract's SECOP docs + contract-level
uploads that are categoria OTROS/unmapped AND unlinked to the cuenta's checklist
rows — read-only, snippet <= 500 chars, never downloads anything. Exposed via
`GET /api/v1/cuentas-cobro/{cuenta_id}/checklist/contexto-docs`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.secop import SecopDocumento
from app.services import checklist_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-CTX-001",
        objeto="Servicios de contexto",
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


@pytest.fixture
async def cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    await checklist_service.asegurar_checklist(db, cuenta=cc)
    await db.commit()
    return cc


def _secop_doc(contrato: Contrato, nombre: str, **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo=nombre,
        datos_raw={},
        **kwargs,
    )


def _upload(
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    nombre: str,
    *,
    cuenta_cobro_id: uuid.UUID | None = None,
    **kwargs: Any,
) -> DocumentoFuente:
    return DocumentoFuente(
        usuario_id=usuario_id,
        contrato_id=contrato_id,
        cuenta_cobro_id=cuenta_cobro_id,
        storage_key=f"docs/{uuid.uuid4()}.pdf",
        nombre=nombre,
        tipo=TipoDocumentoFuente.INSTRUCCIONES,
        **kwargs,
    )


# ── 3.1 service filtering ───────────────────────────────────────────────────


async def test_secop_otros_no_vinculado_aparece_con_snippet(
    db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    doc = _secop_doc(
        contrato,
        "acta_interna.pdf",
        descripcion="Acta administrativa",
        url_descarga="https://s/acta.pdf",
        categoria=CategoriaDocumento.OTROS,
        texto_extraido="x" * 900,
        texto_estado="ok",
    )
    db.add(doc)
    await db.commit()

    items = await checklist_service.listar_documentos_contexto(db, cuenta)

    assert len(items) == 1
    item = items[0]
    assert item["id"] == doc.id
    assert item["fuente"] == "secop"
    assert item["nombre"] == "acta_interna.pdf"
    assert item["descripcion"] == "Acta administrativa"
    assert item["categoria"] == "otros"
    assert item["url_descarga"] == "https://s/acta.pdf"
    assert item["snippet"] == "x" * 500  # <= 500 chars of texto_extraido


async def test_docs_mapeados_o_vinculados_quedan_fuera(
    db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    """A doc whose categoria maps to a requisito is NOT context; neither is an
    OTROS doc already linked to a checklist row of this cuenta."""
    mapeado = _secop_doc(contrato, "contrato firmado.pdf", categoria=CategoriaDocumento.CONTRATO)
    vinculado = _secop_doc(contrato, "anexo_vinculado.pdf", categoria=CategoriaDocumento.OTROS)
    libre = _secop_doc(contrato, "anexo_libre.pdf", categoria=CategoriaDocumento.OTROS)
    db.add_all([mapeado, vinculado, libre])
    await db.commit()
    # User manually assigns the OTROS doc to a requisito → it stops being context.
    await checklist_service.vincular_secop_documento(db, cuenta.id, "DS_CONSECUTIVO", vinculado.id)
    await db.commit()

    items = await checklist_service.listar_documentos_contexto(db, cuenta)

    assert [i["id"] for i in items] == [libre.id]


async def test_uploads_nivel_contrato_otros_aparecen_y_sin_texto_snippet_none(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, cuenta: CuentaCobro
) -> None:
    user_id = test_user["user"].id
    ctx_upload = _upload(user_id, contrato.id, "manual_operativo.pdf", categoria=CategoriaDocumento.OTROS)
    scoped = _upload(
        user_id,
        contrato.id,
        "soporte_mensual.pdf",
        cuenta_cobro_id=cuenta.id,
        categoria=CategoriaDocumento.OTROS,
    )
    db.add_all([ctx_upload, scoped])
    await db.commit()

    items = await checklist_service.listar_documentos_contexto(db, cuenta)

    assert [i["id"] for i in items] == [ctx_upload.id]  # cuenta-scoped upload excluded
    assert items[0]["fuente"] == "upload"
    assert items[0]["snippet"] is None  # no texto_extraido → metadata only
    assert items[0]["url_descarga"] is None


# ── 3.3 endpoint integration ────────────────────────────────────────────────


async def test_endpoint_contexto_docs_devuelve_items(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, cuenta: CuentaCobro
) -> None:
    doc = _secop_doc(
        contrato,
        "acta_interna.pdf",
        categoria=CategoriaDocumento.OTROS,
        texto_extraido="Contenido del acta administrativa interna",
        texto_estado="ok",
    )
    db.add(doc)
    await db.commit()

    resp = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/contexto-docs", headers=test_user["headers"])

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(doc.id)
    assert body[0]["fuente"] == "secop"
    assert body[0]["snippet"] == "Contenido del acta administrativa interna"


async def test_endpoint_contexto_docs_cuenta_ajena_403(
    client: AsyncClient, db: AsyncSession, cuenta: CuentaCobro
) -> None:
    from app.core.security import create_access_token, hash_password
    from app.models.usuario import Usuario

    otro = Usuario(
        email="otro@example.com",
        nombre="Otro",
        cedula="987654321",
        password_hash=hash_password("TestPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro)
    await db.commit()
    await db.refresh(otro)
    token = create_access_token(subject=str(otro.id), role=otro.rol)

    resp = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/contexto-docs",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
