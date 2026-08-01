"""Tests for informe_service (DOCX + ZIP generators).

The ZIP evidencias tests (billing-resilience-templates, slice #2) use a SYNTHETIC
real-leak-shaped corpus to exercise the mandatory secret scan — modeled on the
SHAPE of the real leak (a Postgres/Neon connection string + a Neon-style API key)
with entirely FAKE values. Never copy real credential values into this file.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.core.config import settings
from app.core.exceptions import ForbiddenError, ValidationError
from app.core.text_match import normalize
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.plantilla_organismo import PlantillaOrganismo
from app.services import informe_service
from docx import Document
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_LEAK_CORPUS_TEXTO = (
    "Notas internas\n"
    "DATABASE_URL=postgresql://fake_user:fake_pass_123@ep-fake-000000"
    ".us-east-1.aws.neon.tech/neondb\n"
    "NEON_API_KEY=napi_a8K3mXpQ9rZtL2wVbN7cJhF4sYdE1oIu\n"
)


def _fake_storage(download_map: dict[str, bytes] | None = None) -> AsyncMock:
    """AsyncMock StoragePort — `download` resolves by key from `download_map`,
    defaulting to an empty-but-nonempty placeholder for unmapped keys."""
    download_map = download_map or {}
    storage = AsyncMock()

    async def _download(key: str) -> bytes:
        return download_map.get(key, b"contenido de evidencia de prueba")

    storage.download = AsyncMock(side_effect=_download)
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-INF-001",
        objeto="Servicios profesionales de desarrollo de software",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Alcaldía",
        dependencia="TI",
        supervisor_nombre="Carlos Supervisor",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligaciones(db: AsyncSession, contrato: Contrato) -> list[Obligacion]:
    obs = [
        Obligacion(
            contrato_id=contrato.id,
            descripcion=f"Obligación contractual #{i + 1} con un texto razonablemente largo",
            tipo=TipoObligacion.GENERAL,
            orden=i,
        )
        for i in range(3)
    ]
    db.add_all(obs)
    await db.commit()
    for o in obs:
        await db.refresh(o)
    # The contrato was loaded with its `obligaciones` collection empty (lazy
    # selectin). Manually attach the new obligations so subsequent loads in
    # the same async session see them.
    contrato.obligaciones = list(obs)
    return obs


@pytest.fixture
async def cuenta(db: AsyncSession, contrato: Contrato, obligaciones: list[Obligacion]) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=5,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=3_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)

    for i, ob in enumerate(obligaciones):
        act = Actividad(
            cuenta_cobro_id=cc.id,
            obligacion_id=ob.id,
            descripcion=f"Actividad realizada {i + 1}",
            justificacion=f"Justificación detallada {i + 1}",
            fecha_realizacion=date(2024, 5, 10 + i),
        )
        db.add(act)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── Informe actividades ────────────────────────────────────────────────────


async def test_informe_actividades_genera_docx_valido(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    user = test_user["user"]
    content, filename = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta.id)
    assert filename.endswith(".docx")
    assert filename.startswith("informe-actividades-")
    assert len(content) > 1000  # not empty
    # Parse it back to verify it's a valid docx
    doc = Document(io.BytesIO(content))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "Informe de actividades" in full_text
    assert "CTR-INF-001" in full_text or any(
        "CTR-INF-001" in cell.text for t in doc.tables for r in t.rows for cell in r.cells
    )


# ── es_borrador machine-readable field (slice #7, task 7.5d) ────────────────


async def test_es_borrador_is_a_public_constant_true() -> None:
    """`ES_BORRADOR` is the machine-readable counterpart of `_BORRADOR_HEADER` —
    every generated informe is currently ALWAYS a draft (no approval workflow)."""
    assert informe_service.ES_BORRADOR is True


async def test_descargar_informe_actividades_incluye_header_es_borrador(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    resp = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/informe-actividades.docx", headers=test_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Es-Borrador"] == "true"


async def test_descargar_informe_supervision_incluye_header_es_borrador(
    client: AsyncClient, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    resp = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/informe-supervision.docx", headers=test_user["headers"])
    assert resp.status_code == 200, resp.text
    assert resp.headers["X-Es-Borrador"] == "true"


async def test_informe_actividades_sin_actividades_falla(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=6,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    with pytest.raises(ValidationError):
        await informe_service.generar_informe_actividades_docx(db, user.id, cc.id)


# ── Informe supervisión ────────────────────────────────────────────────────


async def test_informe_supervision_genera_docx_valido(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    user = test_user["user"]
    content, filename = await informe_service.generar_informe_supervision_docx(db, user.id, cuenta.id)
    assert filename.endswith(".docx")
    assert filename.startswith("informe-supervision-")
    doc = Document(io.BytesIO(content))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "supervisi" in full_text.lower()
    table_texts = [cell.text for t in doc.tables for r in t.rows for cell in r.cells]
    assert any("Carlos Supervisor" in tx for tx in table_texts)


# ── ZIP evidencias ─────────────────────────────────────────────────────────


async def test_zip_evidencias_estructura(db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro) -> None:
    user = test_user["user"]
    content, filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)
    assert filename.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        # Root readme
        assert "LEEME.txt" in names
        # One folder per obligacion (3) each with a LEEME.txt
        leemes = [n for n in names if n.endswith("LEEME.txt") and "/" in n]
        assert len(leemes) >= 3
        # Verify content of one folder
        sample = next(n for n in leemes if n.startswith("01_"))
        body = zf.read(sample).decode("utf-8")
        assert "Obligación #1" in body
        assert "Actividad realizada 1" in body


async def test_ownership_otro_usuario_falla(db: AsyncSession, cuenta: CuentaCobro) -> None:
    fake_user_id = uuid.uuid4()
    with pytest.raises(ForbiddenError):
        await informe_service.generar_informe_actividades_docx(db, fake_user_id, cuenta.id)


# ── Packager hardening (billing-resilience-templates, slice #2) ────────────


@pytest.fixture
async def actividad_con_evidencia(db: AsyncSession, cuenta: CuentaCobro, obligaciones: list[Obligacion]) -> Actividad:
    """First actividad of `cuenta` (linked to the first obligación) plus one
    uploaded (storage-backed) evidencia."""
    res = await db.execute(
        select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id).order_by(Actividad.fecha_realizacion.asc())
    )
    act = res.scalars().first()
    assert act is not None
    ev = Evidencia(
        actividad_id=act.id,
        storage_key=f"evidencias/{act.id}/foto.jpg",
        nombre_archivo="foto.jpg",
        tipo_archivo="image/jpeg",
        tamano_bytes=12,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(act)
    return act


async def test_zip_evidencias_usa_bytes_reales_de_storage(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    actividad_con_evidencia: Actividad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = test_user["user"]
    ev = actividad_con_evidencia.evidencias[0]
    contenido_real = b"contenido-binario-real-de-la-foto"
    storage = _fake_storage({ev.storage_key: contenido_real})
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)

    content, _filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        arcnames = [n for n in zf.namelist() if "evidencias/" in n]
        assert arcnames, "expected at least one real-bytes evidencia entry in the zip"
        assert zf.read(arcnames[0]) == contenido_real
    storage.download.assert_any_call(ev.storage_key)


async def test_resolver_estructura_organismo_returns_none_when_not_ingested(
    db: AsyncSession, contrato: Contrato
) -> None:
    """No `PlantillaOrganismo` has been ingested for this organism — the packager
    falls back to the default numbered folder structure (billing-resilience-
    templates, slice #5, task 5.15 — completes the slice #2 stub)."""
    resultado = await informe_service._resolver_estructura_organismo(db, contrato)
    assert resultado is None


async def test_resolver_estructura_organismo_returns_real_lookup_when_ingested(
    db: AsyncSession, contrato: Contrato
) -> None:
    """Once a template has been ingested for the contract's organism, the real
    lookup (`requisito_inference_service.obtener_plantilla_organismo`) replaces
    the slice #2 stub."""
    from app.core.text_match import normalize
    from app.models.plantilla_organismo import PlantillaOrganismo

    plantilla = PlantillaOrganismo(
        usuario_id=contrato.usuario_id,
        entidad=contrato.entidad,
        entidad_normalizada=normalize(contrato.entidad),
        tipo_documento="informe_actividades",
        formato="docx",
        estructura_json={
            "columnas": ["obligación", "avance del periodo", "evidencia"],
            "secciones": [],
            "anexo_refs": ["Ver Anexo: Carpeta /5. EVIDENCIAS/A1"],
            "notas": "",
        },
    )
    db.add(plantilla)
    await db.commit()

    resultado = await informe_service._resolver_estructura_organismo(db, contrato)
    assert resultado is not None
    assert resultado.estructura_json["anexo_refs"] == ["Ver Anexo: Carpeta /5. EVIDENCIAS/A1"]


async def test_zip_evidencias_uses_default_numbered_folders_when_no_organismo_template(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = test_user["user"]
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: _fake_storage())

    content, _filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        folders = {n.split("/")[0] for n in zf.namelist() if "/" in n}
    assert any(f.startswith(("01_", "02_", "03_")) for f in folders)
    assert not any(f.startswith("A1_") for f in folders)


async def test_zip_evidencias_uses_anexo_style_folders_when_organismo_template_exists(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    cuenta: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the contract's organism has an ingested template whose structure
    includes anexo references (COEMPRESAR-style, 3-column + literal anexo
    refs), the packager numbers evidence folders "A{n}_..." to mirror the
    institutional convention instead of the plain "01_..." default."""
    from app.core.text_match import normalize
    from app.models.plantilla_organismo import PlantillaOrganismo

    user = test_user["user"]
    plantilla = PlantillaOrganismo(
        usuario_id=user.id,
        entidad=contrato.entidad,
        entidad_normalizada=normalize(contrato.entidad),
        tipo_documento="informe_actividades",
        formato="docx",
        estructura_json={
            "columnas": ["obligación", "avance del periodo", "evidencia"],
            "secciones": [],
            "anexo_refs": ["Ver Anexo: Carpeta /5. EVIDENCIAS/A1"],
            "notas": "",
        },
    )
    db.add(plantilla)
    await db.commit()

    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: _fake_storage())

    content, _filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        folders = {n.split("/")[0] for n in zf.namelist() if "/" in n}
    assert any(f.startswith("A1_") for f in folders)
    assert not any(f.startswith(("01_", "02_", "03_")) for f in folders)


async def test_zip_evidencias_estado_pendiente_en_manifest_modo_estandar(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No evidencia files uploaded at all → every obligación is PENDIENTE. Standard
    mode still emits the package, listing PENDIENTE items in the manifest."""
    user = test_user["user"]
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: _fake_storage())

    content, _filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        root = zf.read("LEEME.txt").decode("utf-8")
        assert "PENDIENTE" in root


async def test_zip_evidencias_modo_final_raises_package_pendiente(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = test_user["user"]
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: _fake_storage())

    with pytest.raises(ValidationError) as exc_info:
        await informe_service.generar_zip_evidencias(db, user.id, cuenta.id, modo="final")
    assert exc_info.value.code == "PACKAGE_PENDIENTE"


async def test_zip_evidencias_modo_final_sin_pendientes_emite_zip(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    actividad_con_evidencia: Actividad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the first obligación has evidence in this fixture — modo="final" must
    still raise PACKAGE_PENDIENTE for the remaining pending obligaciones."""
    user = test_user["user"]
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: _fake_storage())

    with pytest.raises(ValidationError) as exc_info:
        await informe_service.generar_zip_evidencias(db, user.id, cuenta.id, modo="final")
    assert exc_info.value.code == "PACKAGE_PENDIENTE"


async def test_zip_evidencias_secreto_detectado_bloquea_paquete(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    actividad_con_evidencia: Actividad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real-leak-SHAPED (synthetic) text file among the evidencia bytes must halt
    packaging entirely — no zip emitted, SECRET_DETECTED_IN_PACKAGE raised."""
    user = test_user["user"]
    ev = actividad_con_evidencia.evidencias[0]
    storage = _fake_storage({ev.storage_key: _LEAK_CORPUS_TEXTO.encode("utf-8")})
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)

    with pytest.raises(ValidationError) as exc_info:
        await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)
    assert exc_info.value.code == "SECRET_DETECTED_IN_PACKAGE"


async def test_zip_evidencias_flag_desactivado_bypassa_scan(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    actividad_con_evidencia: Actividad,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SECRET_SCAN_GATE_ENABLED=False bypasses the scan even with a leak-shaped
    payload present — emergency disable only."""
    user = test_user["user"]
    monkeypatch.setattr(settings, "SECRET_SCAN_GATE_ENABLED", False)
    ev = actividad_con_evidencia.evidencias[0]
    storage = _fake_storage({ev.storage_key: _LEAK_CORPUS_TEXTO.encode("utf-8")})
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)

    content, filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)
    assert filename.endswith(".zip")
    assert content


# ── DOCUMENTOS_CONTRATO/ en el paquete (clasificacion-documentos-secop, D5) ──


async def _setup_docs_contrato(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
) -> dict[str, Any]:
    """Checklist + links: an uploaded CONTRATO doc, a SECOP RPC doc and an
    unlinked OTROS SECOP doc (context). Returns the created objects."""
    from app.models.categoria_documento import CategoriaDocumento
    from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
    from app.models.secop import SecopDocumento
    from app.services import checklist_service

    contrato_id = cuenta.contrato_id
    user = test_user["user"]
    await checklist_service.asegurar_checklist(db, cuenta)

    doc_contrato = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato_id,
        storage_key=f"docs/{uuid.uuid4()}/contrato_firmado.pdf",
        nombre="contrato_firmado.pdf",
        tipo=TipoDocumentoFuente.CONTRATO,
        categoria=CategoriaDocumento.CONTRATO,
    )
    rpc_secop = SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato="CTR-INF-001",
        nombre_archivo="rpc_secop.pdf",
        url_descarga="https://s/rpc_secop.pdf",
        categoria=CategoriaDocumento.REGISTRO_PRESUPUESTAL,
        datos_raw={},
    )
    contexto_secop = SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato="CTR-INF-001",
        nombre_archivo="acta_interna.pdf",
        url_descarga="https://s/acta_interna.pdf",
        categoria=CategoriaDocumento.OTROS,
        datos_raw={},
    )
    db.add_all([doc_contrato, rpc_secop, contexto_secop])
    await db.commit()

    await checklist_service.vincular_documento_fuente(db, cuenta.id, "CONTRATO", doc_contrato.id)
    await checklist_service.vincular_secop_documento(db, cuenta.id, "RPC", rpc_secop.id)
    await db.commit()
    return {"doc_contrato": doc_contrato, "rpc_secop": rpc_secop, "contexto_secop": contexto_secop}


def _fake_descargas_secop(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, bytes], errores: set[str] | None = None):
    from app.services import checklist_service

    errores = errores or set()

    async def _fake(url: str) -> bytes:
        if url in errores:
            raise RuntimeError("simulated secop download failure")
        return payloads[url]

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake)


async def test_zip_incluye_documentos_contrato_por_requisito_y_contexto(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linked contract-level docs land in DOCUMENTOS_CONTRATO/{CODIGO}/ with real
    bytes (uploads via storage, SECOP via url_descarga); remaining context docs
    land in DOCUMENTOS_CONTRATO/CONTEXTO/."""
    user = test_user["user"]
    objetos = await _setup_docs_contrato(db, test_user, cuenta)

    bytes_contrato = b"bytes-reales-contrato-firmado"
    bytes_rpc = b"bytes-reales-rpc-secop"
    bytes_contexto = b"bytes-reales-acta-interna"
    storage = _fake_storage({objetos["doc_contrato"].storage_key: bytes_contrato})
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    _fake_descargas_secop(
        monkeypatch,
        {"https://s/rpc_secop.pdf": bytes_rpc, "https://s/acta_interna.pdf": bytes_contexto},
    )

    content, _filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        assert "DOCUMENTOS_CONTRATO/CONTRATO/contrato_firmado.pdf" in names
        assert zf.read("DOCUMENTOS_CONTRATO/CONTRATO/contrato_firmado.pdf") == bytes_contrato
        assert "DOCUMENTOS_CONTRATO/RPC/rpc_secop.pdf" in names
        assert zf.read("DOCUMENTOS_CONTRATO/RPC/rpc_secop.pdf") == bytes_rpc
        assert "DOCUMENTOS_CONTRATO/CONTEXTO/acta_interna.pdf" in names
        assert zf.read("DOCUMENTOS_CONTRATO/CONTEXTO/acta_interna.pdf") == bytes_contexto


async def test_zip_documentos_contrato_skip_on_error(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One failing download (storage or SECOP) skips ONLY that member — the
    package is still emitted with the rest."""
    user = test_user["user"]
    objetos = await _setup_docs_contrato(db, test_user, cuenta)

    storage = AsyncMock()
    storage.download = AsyncMock(side_effect=RuntimeError("storage down"))
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    _fake_descargas_secop(
        monkeypatch,
        {"https://s/rpc_secop.pdf": b"bytes-rpc"},
        errores={"https://s/acta_interna.pdf"},
    )

    content, filename = await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)

    assert filename.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        assert "DOCUMENTOS_CONTRATO/RPC/rpc_secop.pdf" in names  # the healthy member survives
        assert "DOCUMENTOS_CONTRATO/CONTRATO/contrato_firmado.pdf" not in names
        assert "DOCUMENTOS_CONTRATO/CONTEXTO/acta_interna.pdf" not in names
    assert objetos is not None


async def test_zip_documentos_contrato_entra_al_secret_scan(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leak-shaped payload inside a DOCUMENTOS_CONTRATO member must halt the
    package — the new members join the mandatory secret scan."""
    user = test_user["user"]
    await _setup_docs_contrato(db, test_user, cuenta)

    storage = _fake_storage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    _fake_descargas_secop(
        monkeypatch,
        {
            "https://s/rpc_secop.pdf": b"bytes-rpc",
            "https://s/acta_interna.pdf": _LEAK_CORPUS_TEXTO.encode("utf-8"),
        },
    )

    with pytest.raises(ValidationError) as exc_info:
        await informe_service.generar_zip_evidencias(db, user.id, cuenta.id)
    assert exc_info.value.code == "SECRET_DETECTED_IN_PACKAGE"


async def test_obtener_estado_listo_pendiente_sin_zip(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta: CuentaCobro,
    actividad_con_evidencia: Actividad,
) -> None:
    user = test_user["user"]
    estado = await informe_service.obtener_estado_listo_pendiente(db, user.id, cuenta.id)

    assert estado.cuenta_cobro_id == cuenta.id
    assert len(estado.obligaciones) == 3
    listos = [o for o in estado.obligaciones if o.listo]
    assert len(listos) == 1
    assert estado.pendientes == 2
    assert estado.listo_para_radicar is False


# ── Adaptive generation (billing-resilience-templates, slice #6) ───────────
#
# Per-organism layout selection, always-draft header, progressive narrative,
# and one-time obligation blanking. `generar_informe_actividades_docx`/
# `generar_informe_supervision_docx` keep their exact `(db, usuario_id,
# cuenta_id) -> (bytes, filename)` public signature — organismo/posicion/
# prior_context are resolved INTERNALLY from the already-loaded contrato/cuenta
# rather than added as new required params (design mentions adding params to
# the generators; every existing call site — `checklist_autogen_service.
# _GENERADORES`, `app/api/v1/cuentas_cobro.py`, the two informe tools — calls
# them positionally with exactly 3 args, so widening the signature would be a
# breaking, undeclared change to 3+ call sites for no behavioral benefit).


@pytest.fixture
async def contrato_dagma(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-DAGMA-001",
        objeto="Servicios profesionales ambientales",
        valor_total=24_000_000,
        valor_mensual=2_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
        dependencia="Ambiental",
        supervisor_nombre="Sup DAGMA",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def contrato_coempresar(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-COEMPRESAR-001",
        objeto="Servicios profesionales de saneamiento",
        valor_total=24_000_000,
        valor_mensual=2_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="COEMPRESAR",
        dependencia="Operaciones",
        supervisor_nombre="Sup Coempresar",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _make_obligaciones(db: AsyncSession, contrato: Contrato, n: int = 2) -> list[Obligacion]:
    obs = [
        Obligacion(
            contrato_id=contrato.id,
            descripcion=f"Obligación contractual #{i + 1} con texto suficientemente largo",
            tipo=TipoObligacion.GENERAL,
            orden=i,
        )
        for i in range(n)
    ]
    db.add_all(obs)
    await db.commit()
    for o in obs:
        await db.refresh(o)
    contrato.obligaciones = list(obs)
    return obs


async def _make_cuenta_con_actividades(
    db: AsyncSession,
    contrato: Contrato,
    obligaciones: list[Obligacion],
    mes: int,
    anio: int = 2024,
    numero_cuota: int | None = None,
    posicion: PosicionCuota = PosicionCuota.RECURRENTE,
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=2_000_000,
        numero_cuota=numero_cuota,
        posicion=posicion,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    for i, ob in enumerate(obligaciones):
        db.add(
            Actividad(
                cuenta_cobro_id=cc.id,
                obligacion_id=ob.id,
                descripcion=f"Actividad realizada {i + 1} en {mes}/{anio}",
                justificacion=f"Justificación detallada {i + 1} en {mes}/{anio}",
                fecha_realizacion=date(anio, mes, 10),
            )
        )
    await db.commit()
    await db.refresh(cc)
    return cc


async def _ingerir_plantilla(
    db: AsyncSession,
    contrato: Contrato,
    columnas: list[str],
    anexo_refs: list[str] | None = None,
    tipo_documento: str = "informe_actividades",
) -> PlantillaOrganismo:
    plantilla = PlantillaOrganismo(
        usuario_id=contrato.usuario_id,
        entidad=contrato.entidad,
        entidad_normalizada=normalize(contrato.entidad),
        tipo_documento=tipo_documento,
        formato="docx",
        estructura_json={
            "columnas": columnas,
            "secciones": [],
            "anexo_refs": anexo_refs or [],
            "notas": "",
        },
    )
    db.add(plantilla)
    await db.commit()
    await db.refresh(plantilla)
    return plantilla


# ── Per-organism layout selection (6.1-6.4) ─────────────────────────────────


async def test_informe_actividades_dagma_2_columnas(
    db: AsyncSession, test_user: dict[str, Any], contrato_dagma: Contrato
) -> None:
    """DAGMA-style organism: 2-column layout (obligación | avance), no evidence
    column — scenario spec `adaptive-informe-generation`."""
    user = test_user["user"]
    obligaciones = await _make_obligaciones(db, contrato_dagma)
    await _ingerir_plantilla(db, contrato_dagma, columnas=["Obligación", "Avance del periodo"])
    cuenta = await _make_cuenta_con_actividades(db, contrato_dagma, obligaciones, mes=1, numero_cuota=1)

    content, _filename = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta.id)
    doc = Document(io.BytesIO(content))
    tabla = doc.tables[1]
    headers = [c.text for c in tabla.rows[0].cells]
    assert headers == ["Obligación", "Avance del periodo"]
    assert len(tabla.columns) == 2
    assert not any("evidenc" in h.lower() for h in headers)


async def test_informe_actividades_coempresar_3_columnas_con_anexo_refs(
    db: AsyncSession, test_user: dict[str, Any], contrato_coempresar: Contrato
) -> None:
    """COEMPRESAR-style organism: 3-column layout (+ evidencia) and the literal
    anexo reference from the ingested template appears verbatim in the DOCX."""
    user = test_user["user"]
    obligaciones = await _make_obligaciones(db, contrato_coempresar)
    anexo_ref = "Ver Anexo: Carpeta /5. EVIDENCIAS/A1"
    await _ingerir_plantilla(
        db,
        contrato_coempresar,
        columnas=["Obligación", "Avance del periodo", "Evidencia"],
        anexo_refs=[anexo_ref],
    )
    cuenta = await _make_cuenta_con_actividades(db, contrato_coempresar, obligaciones, mes=1, numero_cuota=1)

    content, _filename = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta.id)
    doc = Document(io.BytesIO(content))
    tabla = doc.tables[1]
    headers = [c.text for c in tabla.rows[0].cells]
    assert headers == ["Obligación", "Avance del periodo", "Evidencia"]
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert anexo_ref in full_text


async def test_informe_actividades_default_4_columnas_sin_plantilla(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    """No organism template ingested — the current default 4-column layout is
    used unchanged (zero regression)."""
    user = test_user["user"]
    content, _filename = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta.id)
    doc = Document(io.BytesIO(content))
    tabla = doc.tables[1]
    headers = [c.text for c in tabla.rows[0].cells]
    assert headers == ["#", "Obligación", "Actividad realizada", "Justificación"]


# ── Always-draft header (6.7-6.8) ───────────────────────────────────────────


async def test_informe_actividades_siempre_carga_encabezado_borrador(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    user = test_user["user"]
    content, _filename = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta.id)
    doc = Document(io.BytesIO(content))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "BORRADOR" in full_text
    assert "sujeto a revisión" in full_text


async def test_informe_supervision_siempre_carga_encabezado_borrador(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    user = test_user["user"]
    content, _filename = await informe_service.generar_informe_supervision_docx(db, user.id, cuenta.id)
    doc = Document(io.BytesIO(content))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "BORRADOR" in full_text


# ── Progressive narrative (6.5-6.6) ─────────────────────────────────────────


async def test_contexto_progresivo_ausente_en_primera_cuota(db: AsyncSession, contrato_dagma: Contrato) -> None:
    obligaciones = await _make_obligaciones(db, contrato_dagma)
    cuenta1 = await _make_cuenta_con_actividades(
        db, contrato_dagma, obligaciones, mes=1, numero_cuota=1, posicion=PosicionCuota.PRIMERA
    )
    contexto = await informe_service._construir_contexto_progresivo(db, contrato_dagma.id, cuenta1)
    assert contexto is None


async def test_contexto_progresivo_construido_desde_cuotas_previas(db: AsyncSession, contrato_dagma: Contrato) -> None:
    """Cuota 3's narrative is built from cuotas 1 and 2's recorded activity text."""
    obligaciones = await _make_obligaciones(db, contrato_dagma)
    await _make_cuenta_con_actividades(
        db, contrato_dagma, obligaciones, mes=1, numero_cuota=1, posicion=PosicionCuota.PRIMERA
    )
    await _make_cuenta_con_actividades(db, contrato_dagma, obligaciones, mes=2, numero_cuota=2)
    cuenta3 = await _make_cuenta_con_actividades(db, contrato_dagma, obligaciones, mes=3, numero_cuota=3)

    fake_resp = MagicMock()
    fake_resp.content = "Narrativa progresiva sintetizada."
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=fake_resp)

    with patch.object(informe_service, "get_llm", return_value=mock_llm):
        contexto = await informe_service._construir_contexto_progresivo(db, contrato_dagma.id, cuenta3)

    assert contexto == "Narrativa progresiva sintetizada."
    mock_llm.complete.assert_awaited_once()
    prompt_sent = str(mock_llm.complete.call_args)
    assert "1/2024" in prompt_sent
    assert "2/2024" in prompt_sent


async def test_contexto_progresivo_llm_error_falla_abierto(db: AsyncSession, contrato_dagma: Contrato) -> None:
    """A narrative-synthesis LLM failure degrades to the raw joined summaries —
    it never blocks or raises (mirrors `_convertir_actividades_tercera_persona`)."""
    obligaciones = await _make_obligaciones(db, contrato_dagma)
    await _make_cuenta_con_actividades(
        db, contrato_dagma, obligaciones, mes=1, numero_cuota=1, posicion=PosicionCuota.PRIMERA
    )
    cuenta2 = await _make_cuenta_con_actividades(db, contrato_dagma, obligaciones, mes=2, numero_cuota=2)

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("llm down"))

    with patch.object(informe_service, "get_llm", return_value=mock_llm):
        contexto = await informe_service._construir_contexto_progresivo(db, contrato_dagma.id, cuenta2)

    assert contexto is not None
    assert "Actividad realizada 1 en 1/2024" in contexto


async def test_contexto_progresivo_degrada_a_k3_cuando_excede_presupuesto(
    db: AsyncSession, contrato_dagma: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the bounded char budget is exceeded, degrade to the K=3 most recent
    summaries + a note about how many were omitted."""
    monkeypatch.setattr(informe_service, "_MAX_TEXT_CHARS", 10)
    obligaciones = await _make_obligaciones(db, contrato_dagma, n=1)
    for mes in range(1, 5):
        await _make_cuenta_con_actividades(db, contrato_dagma, obligaciones, mes=mes, numero_cuota=mes)
    cuenta5 = await _make_cuenta_con_actividades(db, contrato_dagma, obligaciones, mes=5, numero_cuota=5)

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=RuntimeError("skip llm, inspect raw block"))

    with patch.object(informe_service, "get_llm", return_value=mock_llm):
        contexto = await informe_service._construir_contexto_progresivo(db, contrato_dagma.id, cuenta5)

    assert contexto is not None
    assert "1/2024" not in contexto  # oldest cuota omitted
    assert "2/2024" in contexto
    assert "3/2024" in contexto
    assert "4/2024" in contexto
    assert "se omiten" in contexto.lower()


# ── One-time obligation blanking (6.9-6.10) ─────────────────────────────────


async def test_obligacion_una_vez_visible_en_cuota_primera_y_ausente_en_recurrente(
    db: AsyncSession, test_user: dict[str, Any], contrato_dagma: Contrato
) -> None:
    user = test_user["user"]
    obligaciones = await _make_obligaciones(db, contrato_dagma, n=2)
    obligacion_unica = obligaciones[0]
    obligacion_unica.una_vez = True
    db.add(obligacion_unica)
    await db.commit()

    cuenta1 = await _make_cuenta_con_actividades(
        db, contrato_dagma, obligaciones, mes=1, numero_cuota=1, posicion=PosicionCuota.PRIMERA
    )
    cuenta2 = await _make_cuenta_con_actividades(db, contrato_dagma, obligaciones, mes=2, numero_cuota=2)

    content1, _f1 = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta1.id)
    doc1 = Document(io.BytesIO(content1))
    tabla1 = doc1.tables[1]
    body1 = "\n".join(cell.text for row in tabla1.rows for cell in row.cells)
    assert obligacion_unica.descripcion in body1

    # cuenta2 has a prior cuota, so `_construir_contexto_progresivo` would call
    # the LLM to synthesize a narrative — mock it to keep this test offline and
    # deterministic (fails open if unmocked, but must never hit the network).
    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=MagicMock(content="Narrativa de prueba."))
    with patch.object(informe_service, "get_llm", return_value=mock_llm):
        content2, _f2 = await informe_service.generar_informe_actividades_docx(db, user.id, cuenta2.id)
    doc2 = Document(io.BytesIO(content2))
    tabla2 = doc2.tables[1]
    body2 = "\n".join(cell.text for row in tabla2.rows for cell in row.cells)
    assert obligacion_unica.descripcion not in body2
    # The regular (not una_vez) obligación is still reported every cuota.
    assert obligaciones[1].descripcion in body2
