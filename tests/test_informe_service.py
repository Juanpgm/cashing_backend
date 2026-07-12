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
from unittest.mock import AsyncMock

import pytest
from app.core.config import settings
from app.core.exceptions import ForbiddenError, ValidationError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import informe_service
from docx import Document
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
