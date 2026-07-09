"""Tests for multi-file, any-format evidence uploads.

Covers:
- Batch validation in `app.core.file_validation.validate_evidence_file` (permissive
  allowlist: any format is accepted except a blocklist of executables/scripts).
- `evidencia_service.subir_evidencias` (new batch function) — validates ALL files
  before persisting ANY of them (fail-fast, all-or-nothing).
- The `POST /evidencias/actividades/{id}` endpoint accepting multiple files.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.exceptions import ValidationError
from app.core.file_validation import (
    MAX_EVIDENCE_FILE_SIZE_BYTES,
    MAX_EVIDENCE_FILES_PER_REQUEST,
    validate_evidence_file,
)
from app.core.security import hash_password
from app.main import app as fastapi_app
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.usuario import Usuario
from app.services import evidencia_service
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PDF_MAGIC = b"%PDF-1.4 sample pdf content here"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-EVI-FMT-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="SENA",
        dependencia="Sistemas",
        supervisor_nombre="Pedro",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=3,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=3_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


@pytest.fixture
async def actividad(db: AsyncSession, cuenta_cobro: CuentaCobro) -> Actividad:
    a = Actividad(
        cuenta_cobro_id=cuenta_cobro.id,
        descripcion="Reunión de seguimiento",
        fecha_realizacion=date(2024, 3, 15),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


@pytest.fixture
async def actividad_ajena(db: AsyncSession) -> Actividad:
    """An actividad belonging to a DIFFERENT user (not `test_user`) — used to verify
    cross-tenant ownership checks on the evidencias endpoints."""
    otro_user = Usuario(
        email="otro-usuario-evi@example.com",
        nombre="Otro Usuario",
        cedula="987654321",
        telefono="+573009876543",
        password_hash=hash_password("OtherPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(otro_user)
    await db.commit()
    await db.refresh(otro_user)

    otro_contrato = Contrato(
        usuario_id=otro_user.id,
        numero_contrato="CTR-EVI-AJENO-001",
        objeto="Prestación de servicios de consultoría",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="SENA",
        dependencia="Sistemas",
        supervisor_nombre="Ana",
    )
    db.add(otro_contrato)
    await db.commit()
    await db.refresh(otro_contrato)

    otro_cuenta = CuentaCobro(
        contrato_id=otro_contrato.id,
        mes=4,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(otro_cuenta)
    await db.commit()
    await db.refresh(otro_cuenta)

    otra_actividad = Actividad(
        cuenta_cobro_id=otro_cuenta.id,
        descripcion="Actividad de otro usuario",
        fecha_realizacion=date(2024, 4, 10),
    )
    db.add(otra_actividad)
    await db.commit()
    await db.refresh(otra_actividad)
    return otra_actividad


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload.return_value = "evidencias/test/key"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


# ── Unit tests: validate_evidence_file ───────────────────────────────────────


def test_validate_evidence_file_accepts_heic() -> None:
    validate_evidence_file(
        filename="foto.heic",
        size=1024,
        content_type="image/heic",
        content=b"\x00\x00\x00\x18ftypheic",
    )


def test_validate_evidence_file_accepts_mp4() -> None:
    validate_evidence_file(
        filename="clip.mp4",
        size=2048,
        content_type="video/mp4",
        content=b"\x00\x00\x00\x18ftypmp42",
    )


def test_validate_evidence_file_accepts_eml() -> None:
    validate_evidence_file(
        filename="correo.eml",
        size=512,
        content_type="message/rfc822",
        content=b"From: a@b.com\nSubject: test\n\nBody",
    )


def test_validate_evidence_file_accepts_zip() -> None:
    validate_evidence_file(
        filename="archivo.zip",
        size=64,
        content_type="application/zip",
        content=b"PK\x03\x04rest-of-zip",
    )


def test_validate_evidence_file_accepts_csv() -> None:
    validate_evidence_file(
        filename="datos.csv",
        size=32,
        content_type="text/csv",
        content=b"a,b,c\n1,2,3",
    )


@pytest.mark.parametrize("filename", ["virus.exe", "script.ps1", "installer.bat", "shell.sh", "malware.js"])
def test_validate_evidence_file_rejects_blocked_extension(filename: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        validate_evidence_file(
            filename=filename,
            size=100,
            content_type="application/octet-stream",
            content=b"MZ-fake-binary",
        )
    assert filename in str(exc_info.value.detail)


def test_validate_evidence_file_rejects_trailing_dot_bypass() -> None:
    """'malware.exe.' must be treated as a .exe file (trailing dot stripped before
    extension extraction), not as a file with an empty/'.' extension that evades
    the blocklist."""
    with pytest.raises(ValidationError):
        validate_evidence_file(
            filename="malware.exe.",
            size=100,
            content_type="application/octet-stream",
            content=b"MZ-fake-binary",
        )


def test_validate_evidence_file_rejects_blocked_extension_uppercase() -> None:
    """Casing must not evade the blocklist either."""
    with pytest.raises(ValidationError):
        validate_evidence_file(
            filename="MALWARE.EXE",
            size=100,
            content_type="application/octet-stream",
            content=b"MZ-fake-binary",
        )


def test_validate_evidence_file_rejects_double_extension_trick() -> None:
    """foto.jpg.exe must be rejected based on its FINAL extension after sanitization."""
    with pytest.raises(ValidationError):
        validate_evidence_file(
            filename="foto.jpg.exe",
            size=100,
            content_type="image/jpeg",
            content=b"\xff\xd8\xff-fake",
        )


def test_validate_evidence_file_rejects_empty_file() -> None:
    with pytest.raises(ValidationError):
        validate_evidence_file(
            filename="vacio.pdf",
            size=0,
            content_type="application/pdf",
            content=b"",
        )


def test_validate_evidence_file_rejects_oversized_file() -> None:
    with pytest.raises(ValidationError):
        validate_evidence_file(
            filename="grande.mp4",
            size=MAX_EVIDENCE_FILE_SIZE_BYTES + 1,
            content_type="video/mp4",
            content=b"\x00\x00\x00\x18ftypmp42",
        )


def test_validate_evidence_file_accepts_exactly_max_size() -> None:
    validate_evidence_file(
        filename="justo.mp4",
        size=MAX_EVIDENCE_FILE_SIZE_BYTES,
        content_type="video/mp4",
        content=b"\x00\x00\x00\x18ftypmp42",
    )


def test_validate_evidence_file_rejects_pdf_with_wrong_magic_bytes() -> None:
    """A file named *.pdf must match the PDF magic bytes — extension alone isn't trusted."""
    with pytest.raises(ValidationError):
        validate_evidence_file(
            filename="fake.pdf",
            size=20,
            content_type="application/pdf",
            content=b"this-is-not-a-pdf!!",
        )


def test_validate_evidence_file_accepts_valid_pdf_magic_bytes() -> None:
    validate_evidence_file(
        filename="real.pdf",
        size=len(_PDF_MAGIC),
        content_type="application/pdf",
        content=_PDF_MAGIC,
    )


# ── Service tests: subir_evidencias (batch) ──────────────────────────────────


async def test_subir_evidencias_crea_n_registros(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    archivos = [
        ("foto.heic", "image/heic", b"\x00\x00\x00\x18ftypheic"),
        ("clip.mp4", "video/mp4", b"\x00\x00\x00\x18ftypmp42"),
        ("correo.eml", "message/rfc822", b"From: a@b.com\nSubject: x\n\nBody"),
    ]

    resultados = await evidencia_service.subir_evidencias(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        archivos=archivos,
    )

    assert len(resultados) == 3
    assert {r.nombre_archivo for r in resultados} == {"foto.heic", "clip.mp4", "correo.eml"}

    evidencias = await evidencia_service.listar_evidencias(db, user.id, actividad.id)
    assert len(evidencias) == 3


async def test_subir_evidencias_single_file_still_works(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    resultados = await evidencia_service.subir_evidencias(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=actividad.id,
        archivos=[("informe.pdf", "application/pdf", _PDF_MAGIC)],
    )
    assert len(resultados) == 1
    assert resultados[0].nombre_archivo == "informe.pdf"


async def test_subir_evidencias_un_archivo_invalido_rechaza_todo_el_lote(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    """One bad file in the batch must reject the WHOLE request — nothing persisted."""
    user = test_user["user"]
    storage = _mock_storage()
    archivos = [
        ("foto.heic", "image/heic", b"\x00\x00\x00\x18ftypheic"),
        ("virus.exe", "application/octet-stream", b"MZ-fake-binary"),
    ]

    with pytest.raises(ValidationError) as exc_info:
        await evidencia_service.subir_evidencias(
            db=db,
            storage=storage,
            usuario_id=user.id,
            actividad_id=actividad.id,
            archivos=archivos,
        )
    assert "virus.exe" in str(exc_info.value.detail)

    storage.upload.assert_not_called()
    result = await db.execute(select(Evidencia).where(Evidencia.actividad_id == actividad.id))
    assert result.scalars().all() == []


async def test_subir_evidencias_archivo_muy_grande_rechazado(
    db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    user = test_user["user"]
    storage = _mock_storage()
    archivo_grande = b"x" * (MAX_EVIDENCE_FILE_SIZE_BYTES + 1)

    with pytest.raises(ValidationError):
        await evidencia_service.subir_evidencias(
            db=db,
            storage=storage,
            usuario_id=user.id,
            actividad_id=actividad.id,
            archivos=[("grande.mp4", "video/mp4", archivo_grande)],
        )
    storage.upload.assert_not_called()


# ── API tests: POST /evidencias/actividades/{id} (multi-file) ────────────────


def _mock_evidencia_storage() -> Any:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="evidencias/fake/key")
    storage.presigned_url = AsyncMock(return_value="https://s3.example.com/presigned")
    return storage


async def test_endpoint_sube_multiples_archivos(
    client: AsyncClient, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad.id}",
            headers=test_user["headers"],
            files=[
                ("files", ("foto.heic", b"\x00\x00\x00\x18ftypheic", "image/heic")),
                ("files", ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")),
            ],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 201
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 2
    assert {item["nombre_archivo"] for item in data} == {"foto.heic", "clip.mp4"}


async def test_endpoint_single_file_still_works(
    client: AsyncClient, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad.id}",
            headers=test_user["headers"],
            files=[("files", ("informe.pdf", _PDF_MAGIC, "application/pdf"))],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 201
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["nombre_archivo"] == "informe.pdf"


async def test_endpoint_rechaza_ejecutable_con_nombre_de_archivo_en_detalle(
    client: AsyncClient, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad.id}",
            headers=test_user["headers"],
            files=[("files", ("virus.exe", b"MZ-fake-binary", "application/octet-stream"))],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 422
    assert "virus.exe" in resp.json()["detail"]


async def test_endpoint_un_archivo_invalido_en_lote_rechaza_todo(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad.id}",
            headers=test_user["headers"],
            files=[
                ("files", ("foto.heic", b"\x00\x00\x00\x18ftypheic", "image/heic")),
                ("files", ("script.ps1", b"malicious-script", "application/octet-stream")),
            ],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 422
    result = await db.execute(select(Evidencia).where(Evidencia.actividad_id == actividad.id))
    assert result.scalars().all() == []


async def test_endpoint_rechaza_mas_de_10_archivos(
    client: AsyncClient, test_user: dict[str, Any], actividad: Actividad
) -> None:
    """More than MAX_EVIDENCE_FILES_PER_REQUEST files in one request must 422
    BEFORE any file content is read/stored."""
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        files = [
            ("files", (f"foto{i}.heic", b"\x00\x00\x00\x18ftypheic", "image/heic"))
            for i in range(MAX_EVIDENCE_FILES_PER_REQUEST + 1)
        ]
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad.id}",
            headers=test_user["headers"],
            files=files,
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 422


async def test_endpoint_acepta_exactamente_el_limite_de_archivos(
    client: AsyncClient, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        files = [
            ("files", (f"foto{i}.heic", b"\x00\x00\x00\x18ftypheic", "image/heic"))
            for i in range(MAX_EVIDENCE_FILES_PER_REQUEST)
        ]
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad.id}",
            headers=test_user["headers"],
            files=files,
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 201
    assert len(resp.json()) == MAX_EVIDENCE_FILES_PER_REQUEST


async def test_endpoint_subir_evidencia_actividad_de_otro_usuario_da_404(
    client: AsyncClient, test_user: dict[str, Any], actividad_ajena: Actividad
) -> None:
    """User A must not be able to upload evidence to user B's actividad."""
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad_ajena.id}",
            headers=test_user["headers"],
            files=[("files", ("informe.pdf", _PDF_MAGIC, "application/pdf"))],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 404


async def test_endpoint_listar_evidencias_actividad_de_otro_usuario_da_404(
    client: AsyncClient, test_user: dict[str, Any], actividad_ajena: Actividad
) -> None:
    """User A must not be able to list user B's actividad evidencias."""
    resp = await client.get(
        f"/api/v1/evidencias/actividades/{actividad_ajena.id}",
        headers=test_user["headers"],
    )

    assert resp.status_code == 404


async def test_endpoint_rechaza_archivo_muy_grande(
    client: AsyncClient, test_user: dict[str, Any], actividad: Actividad
) -> None:
    from app.api.v1.evidencias import get_evidencia_storage

    fastapi_app.dependency_overrides[get_evidencia_storage] = _mock_evidencia_storage
    try:
        archivo_grande = b"x" * (MAX_EVIDENCE_FILE_SIZE_BYTES + 1)
        resp = await client.post(
            f"/api/v1/evidencias/actividades/{actividad.id}",
            headers=test_user["headers"],
            files=[("files", ("grande.mp4", archivo_grande, "video/mp4"))],
        )
    finally:
        fastapi_app.dependency_overrides.pop(get_evidencia_storage, None)

    assert resp.status_code == 422
