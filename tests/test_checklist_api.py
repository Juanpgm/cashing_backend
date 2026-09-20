"""Tests for the checklist API (/cuentas-cobro/{id}/checklist)."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.secop import SecopDocumento
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PDF_MAGIC = b"%PDF-1.4 sample pdf content here"


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-CHK-API-001",
        objeto="Servicios checklist API",
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
        requisitos_modo="estandar",  # checklist already defined (gate resolved)
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def cuenta_sin_definir(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    """A freshly-created cuenta whose checklist gate has NOT been resolved."""
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=2,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def test_get_checklist_seeds_and_returns_items(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "resumen" in body
    assert "arbol_evidencias" in body
    codigos = {item["requisito"]["codigo"] for item in body["items"]}
    assert "CONTRATO" in codigos
    assert "RPC" in codigos
    assert body["resumen"]["radicacion_lista"] is False


async def test_patch_no_aplica(client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro) -> None:
    # Seed via GET first
    await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )

    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/DS_CONSECUTIVO",
        headers=test_user["headers"],
        json={"no_aplica": True, "observaciones": "No aplica este mes."},
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["estado"] == "no_aplica"
    assert item["observaciones"] == "No aplica este mes."


async def test_patch_cumplido_manual(client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro) -> None:
    await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )

    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/CEDULA",
        headers=test_user["headers"],
        json={"cumplido_manual": True},
    )
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "cumplido_manual"


async def test_patch_rejects_multiple_actions(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )

    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/CEDULA",
        headers=test_user["headers"],
        json={"no_aplica": True, "cumplido_manual": True},
    )
    assert r.status_code in (400, 422)


async def test_get_checklist_gate_when_undefined(
    client: AsyncClient, test_user: dict[str, Any], cuenta_sin_definir: CuentaCobro
) -> None:
    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_sin_definir.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requisitos_definidos"] is False
    assert body["items"] == []


async def test_definir_estandar_then_get_returns_items(
    client: AsyncClient, test_user: dict[str, Any], cuenta_sin_definir: CuentaCobro
) -> None:
    d = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta_sin_definir.id}/requisitos",
        headers=test_user["headers"],
        json={"modo": "estandar", "requisitos": []},
    )
    assert d.status_code == 200, d.text

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta_sin_definir.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requisitos_definidos"] is True
    codigos = {i["requisito"]["codigo"] for i in body["items"]}
    assert "CONTRATO" in codigos


async def test_definir_augment_with_custom_and_patch_by_uuid(
    client: AsyncClient, test_user: dict[str, Any], cuenta_sin_definir: CuentaCobro
) -> None:
    cid = cuenta_sin_definir.id
    d = await client.post(
        f"/api/v1/cuentas-cobro/{cid}/requisitos",
        headers=test_user["headers"],
        json={
            "modo": "augment",
            "requisitos": [
                {
                    "codigo": "POLIZA_CUMPLIMIENTO",
                    "etiqueta": "Póliza de cumplimiento",
                    "obligatorio": True,
                    "keywords_deteccion": ["poliza", "cumplimiento"],
                }
            ],
        },
    )
    assert d.status_code == 200, d.text

    r = await client.get(f"/api/v1/cuentas-cobro/{cid}/checklist", headers=test_user["headers"])
    body = r.json()
    custom = next(i for i in body["items"] if i["requisito"]["codigo"] == "POLIZA_CUMPLIMIENTO")
    assert custom["requisito"]["origen"] == "cuenta"
    ref = custom["requisito"]["requisito_cuenta_id"]
    assert ref

    # PATCH the custom row by its UUID
    p = await client.patch(
        f"/api/v1/cuentas-cobro/{cid}/checklist/{ref}",
        headers=test_user["headers"],
        json={"cumplido_manual": True},
    )
    assert p.status_code == 200, p.text
    assert p.json()["estado"] == "cumplido_manual"


async def test_get_checklist_ownership_404(client: AsyncClient, test_user: dict[str, Any]) -> None:
    fake = uuid.uuid4()
    r = await client.get(
        f"/api/v1/cuentas-cobro/{fake}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 404


async def test_refresh_secop_runs(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    contrato: Contrato,
    cuenta: CuentaCobro,
) -> None:
    doc = SecopDocumento(
        id_documento_secop="DOC-API-1",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="Contrato minuta clausulado.pdf",
        descripcion="Contrato",
        datos_raw={},
    )
    db.add(doc)
    await db.commit()

    r = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/refresh-secop",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    contrato_item = next(i for i in body["items"] if i["requisito"]["codigo"] == "CONTRATO")
    assert contrato_item["estado"] in ("detectado", "cargado")
    assert contrato_item.get("secop_documento") is not None or contrato_item.get("documento_fuente") is not None


# ── 1:N document links per requisito (PATCH) ────────────────────────────────


async def _crear_documento_fuente(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    cuenta: CuentaCobro,
    nombre: str,
) -> DocumentoFuente:
    df = DocumentoFuente(
        usuario_id=test_user["user"].id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        storage_key=f"k/{nombre}",
        nombre=nombre,
        tipo=TipoDocumentoFuente.RPC,
    )
    db.add(df)
    await db.commit()
    await db.refresh(df)
    return df


async def test_patch_link_multiple_documentos_fuente_agrega_todos(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    """Linking 3 different documents to RPC via PATCH must keep all 3 (append, not overwrite)."""
    df1 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-original.pdf")
    df2 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-adicion-1.pdf")
    df3 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-adicion-2.pdf")
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])

    for df in (df1, df2, df3):
        r = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
            headers=test_user["headers"],
            json={"documento_fuente_id": str(df.id)},
        )
        assert r.status_code == 200, r.text

    item = r.json()
    assert item["estado"] == "cargado"
    assert item["documento_fuente"]["id"] == str(df1.id)
    ids = [d["id"] for d in item["documentos_fuente"]]
    assert ids == [str(df1.id), str(df2.id), str(df3.id)]


async def test_patch_link_same_documento_dos_veces_es_idempotente(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    df = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc.pdf")
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])

    for _ in range(2):
        r = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
            headers=test_user["headers"],
            json={"documento_fuente_id": str(df.id)},
        )
        assert r.status_code == 200, r.text

    item = r.json()
    assert len(item["documentos_fuente"]) == 1


async def test_patch_desvincular_documento_fuente_id_removes_one_and_keeps_others(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    df1 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    df3 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-3.pdf")
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])
    for df in (df1, df2, df3):
        await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
            headers=test_user["headers"],
            json={"documento_fuente_id": str(df.id)},
        )

    # Unlink a NON-primary document.
    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
        headers=test_user["headers"],
        json={"desvincular_documento_fuente_id": str(df2.id)},
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["estado"] == "cargado"
    ids = {d["id"] for d in item["documentos_fuente"]}
    assert ids == {str(df1.id), str(df3.id)}


async def test_patch_desvincular_primario_promueve_siguiente(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    df1 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])
    for df in (df1, df2):
        await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
            headers=test_user["headers"],
            json={"documento_fuente_id": str(df.id)},
        )

    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
        headers=test_user["headers"],
        json={"desvincular_documento_fuente_id": str(df1.id)},
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["documento_fuente"]["id"] == str(df2.id)
    assert item["estado"] == "cargado"


async def test_patch_desvincular_todos_vuelve_a_pendiente(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    df1 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-1.pdf")
    df2 = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc-2.pdf")
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])
    for df in (df1, df2):
        await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
            headers=test_user["headers"],
            json={"documento_fuente_id": str(df.id)},
        )

    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
        headers=test_user["headers"],
        json={"desvincular": True},
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["estado"] == "pendiente"
    assert item["documentos_fuente"] == []
    assert item["documento_fuente"] is None


async def test_patch_rejects_desvincular_specific_combined_with_another_action(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    df = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc.pdf")
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])
    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
        headers=test_user["headers"],
        json={"desvincular_documento_fuente_id": str(df.id), "no_aplica": True},
    )
    assert r.status_code in (400, 422)


async def test_secop_link_no_longer_clears_documento_fuente(
    client: AsyncClient, test_user: dict[str, Any], db: AsyncSession, contrato: Contrato, cuenta: CuentaCobro
) -> None:
    df = await _crear_documento_fuente(db, test_user, contrato, cuenta, "rpc.pdf")
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])
    await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
        headers=test_user["headers"],
        json={"documento_fuente_id": str(df.id)},
    )

    sd = SecopDocumento(
        id_documento_secop="DOC-API-MIX-1",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo="rpc-secop.pdf",
        descripcion="RPC",
        datos_raw={},
    )
    db.add(sd)
    await db.commit()
    await db.refresh(sd)

    r = await client.patch(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist/RPC",
        headers=test_user["headers"],
        json={"secop_documento_id": str(sd.id)},
    )
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["documento_fuente"]["id"] == str(df.id)  # not cleared
    assert item["secop_documento"]["id"] == str(sd.id)
    assert item["estado"] == "cargado"  # uploaded doc still outranks detection


async def test_upload_batch_3_files_to_rpc_creates_3_vinculos(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    """Batch upload of 3 files against the same requisito must NOT last-write-wins:
    all 3 become linked documents (the first becomes primary)."""
    await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/checklist", headers=test_user["headers"])
    r = await client.post(
        "/api/v1/documentos/upload-batch",
        headers=test_user["headers"],
        params={
            "tipo": "rpc",
            "cuenta_cobro_id": str(cuenta.id),
            "requisito_codigo": "RPC",
        },
        files=[
            ("files", ("rpc-1.pdf", _PDF_MAGIC, "application/pdf")),
            ("files", ("rpc-2.pdf", _PDF_MAGIC, "application/pdf")),
            ("files", ("rpc-3.pdf", _PDF_MAGIC, "application/pdf")),
        ],
    )
    assert r.status_code == 201, r.text
    uploaded = r.json()
    assert len(uploaded) == 3

    g = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )
    assert g.status_code == 200, g.text
    item = next(i for i in g.json()["items"] if i["requisito"]["codigo"] == "RPC")
    assert item["estado"] == "cargado"
    assert len(item["documentos_fuente"]) == 3
    uploaded_ids = {d["id"] for d in uploaded}
    assert {d["id"] for d in item["documentos_fuente"]} == uploaded_ids


# ── GET checklist 500 on link-evidence with null tipo_archivo/tamano_bytes ──
#
# Bug: link evidence created by the Gmail/Drive/Calendar discovery agent
# (evidence_persist_service.py) sets tipo_archivo=None, tamano_bytes=None on
# purpose (there is no file, only a URL). ArbolEvidenciaItem declared both as
# required non-nullable str/int, so pydantic raised a ValidationError inside
# `ChecklistResponse(**payload)` and it propagated as an UNHANDLED 500 — every
# subsequent GET on a cuenta that has ever run evidence discovery broke
# permanently. This must return 200 with tipo_archivo/tamano_bytes as null.


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación contractual con evidencias de prueba",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    return ob


async def _make_actividad(db: AsyncSession, cuenta: CuentaCobro, obligacion: Obligacion) -> Actividad:
    act = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad de prueba",
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)
    return act


async def test_get_checklist_with_link_evidencia_null_tipo_y_tamano_returns_200(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Empty/null input: link-evidence (Gmail/Drive/Calendar discovery) has both
    tipo_archivo and tamano_bytes NULL by design. GET must not 500."""
    act = await _make_actividad(db, cuenta, obligacion)
    evidencia = Evidencia(
        actividad_id=act.id,
        fuente="gmail",
        url="https://mail.google.com/mail/u/0/#inbox/abc123",
        nombre_archivo="Correo de soporte",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    obl = next(o for o in body["arbol_evidencias"] if o["obligacion_id"] == str(obligacion.id))
    actividad = next(a for a in obl["actividades"] if a["id"] == str(act.id))
    assert len(actividad["evidencias"]) == 1
    ev = actividad["evidencias"][0]
    assert ev["tipo_archivo"] is None
    assert ev["tamano_bytes"] is None
    assert ev["nombre_archivo"] == "Correo de soporte"


async def test_get_checklist_link_evidencia_exposes_fuente_y_url(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """A link-evidencia (Gmail/Drive/Calendar discovery) has no stored file, so
    fuente/url are the ONLY way to identify/open it from the árbol de evidencias.
    Regression: these were silently dropped by ArbolEvidenciaItem/
    listar_arbol_evidencias even though the ORM row carries real values
    (mirrors the fuente/url fix already applied to EvidenciaResponse)."""
    act = await _make_actividad(db, cuenta, obligacion)
    evidencia = Evidencia(
        actividad_id=act.id,
        fuente="gmail",
        url="https://mail.google.com/mail/u/0/#inbox/abc123",
        nombre_archivo="Correo de soporte",
        storage_key=None,
        tipo_archivo=None,
        tamano_bytes=None,
    )
    db.add(evidencia)
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    obl = next(o for o in body["arbol_evidencias"] if o["obligacion_id"] == str(obligacion.id))
    actividad = next(a for a in obl["actividades"] if a["id"] == str(act.id))
    assert len(actividad["evidencias"]) == 1
    ev = actividad["evidencias"][0]
    assert ev["fuente"] == "gmail"
    assert ev["url"] == "https://mail.google.com/mail/u/0/#inbox/abc123"


async def test_get_checklist_arbol_evidencias_mixed_file_and_link(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Boundary: mixed evidencias in the same actividad — one uploaded file
    (both fields set) and one link (both fields null) — both must serialize."""
    act = await _make_actividad(db, cuenta, obligacion)
    db.add_all(
        [
            Evidencia(
                actividad_id=act.id,
                fuente=None,
                url=None,
                nombre_archivo="soporte.pdf",
                storage_key="k/soporte.pdf",
                tipo_archivo="application/pdf",
                tamano_bytes=2048,
            ),
            Evidencia(
                actividad_id=act.id,
                fuente="drive",
                url="https://drive.google.com/file/d/xyz",
                nombre_archivo="Evidencia en Drive",
                storage_key=None,
                tipo_archivo=None,
                tamano_bytes=None,
            ),
        ]
    )
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    obl = next(o for o in r.json()["arbol_evidencias"] if o["obligacion_id"] == str(obligacion.id))
    actividad = next(a for a in obl["actividades"] if a["id"] == str(act.id))
    evidencias_by_nombre = {e["nombre_archivo"]: e for e in actividad["evidencias"]}
    assert evidencias_by_nombre["soporte.pdf"]["tipo_archivo"] == "application/pdf"
    assert evidencias_by_nombre["soporte.pdf"]["tamano_bytes"] == 2048
    assert evidencias_by_nombre["Evidencia en Drive"]["tipo_archivo"] is None
    assert evidencias_by_nombre["Evidencia en Drive"]["tamano_bytes"] is None


async def test_get_checklist_actividad_con_cero_evidencias(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """Boundary: an actividad with zero evidencias -> empty list, still 200."""
    act = await _make_actividad(db, cuenta, obligacion)

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
    obl = next(o for o in r.json()["arbol_evidencias"] if o["obligacion_id"] == str(obligacion.id))
    actividad = next(a for a in obl["actividades"] if a["id"] == str(act.id))
    assert actividad["evidencias"] == []


async def test_get_checklist_cuenta_ya_radicada_still_returns_200_not_500(
    client: AsyncClient,
    test_user: dict[str, Any],
    db: AsyncSession,
    cuenta: CuentaCobro,
    obligacion: Obligacion,
) -> None:
    """State-machine: a cuenta already radicada (estado=ENVIADA, the state
    `radicar_cuenta` transitions into) with legacy link-evidence must still be
    readable via GET, not 500. The endpoint has no estado gate today — this
    guards that re-reading the checklist after the state transition keeps
    working once the null-field bug is fixed."""
    act = await _make_actividad(db, cuenta, obligacion)
    db.add(
        Evidencia(
            actividad_id=act.id,
            fuente="calendar",
            url="https://calendar.google.com/event?eid=abc",
            nombre_archivo="Reunión de seguimiento",
            storage_key=None,
            tipo_archivo=None,
            tamano_bytes=None,
        )
    )
    cuenta.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    r = await client.get(
        f"/api/v1/cuentas-cobro/{cuenta.id}/checklist",
        headers=test_user["headers"],
    )
    assert r.status_code == 200, r.text
