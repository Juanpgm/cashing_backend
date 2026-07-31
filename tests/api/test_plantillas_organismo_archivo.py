"""Tests for archive ingestion endpoints (formato-replicacion-inteligente,
slice 1, tasks 2.1-2.8): `POST .../plantillas-organismo/archivo:analizar` and
`archivo:confirmar`.
"""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from datetime import date
from typing import Any

import pytest
from app.core.file_validation import MAX_FILE_SIZE_BYTES
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.plantilla_organismo import PlantillaOrganismo
from app.schemas.agent import LLMResponse
from app.schemas.plantilla_organismo import MiembroConfirmado
from app.services import requisito_inference_service as svc
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ── Fixtures / helpers ──────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-ARCHIVO-001",
        objeto="Servicios profesionales — organismo DAGMA",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


class _InMemoryStorage:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self.store[key] = data
        return key

    async def download(self, key: str) -> bytes:
        if key not in self.store:
            raise FileNotFoundError(f'Could not find "{key}" in local storage.')
        return self.store[key]

    async def delete(self, key: str) -> None:
        self.store.pop(key, None)


def _patch_storage(monkeypatch: pytest.MonkeyPatch) -> _InMemoryStorage:
    storage = _InMemoryStorage()
    monkeypatch.setattr("app.services.document_service._get_storage", lambda *_a, **_kw: storage)
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_kw: storage)
    return storage


class _FakeLLM:
    """Returns `{}` for every structured call — every response_format schema
    in this flow (classification, estructura, campos) has all-optional
    fields, so `{}` degrades to empty defaults without raising."""

    async def complete(self, messages, temperature=0.0, max_tokens=4096, response_format=None, **kwargs) -> LLMResponse:
        return LLMResponse(content="{}", model="fake/test-model", total_tokens=5)


def _patch_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.adapters.llm.get_llm", lambda model=None: _FakeLLM(), raising=True)


def _build_docx_bytes(texto: str = "Contrato No. ABC-123 informe de actividades") -> bytes:
    from docx import Document

    doc = Document()
    doc.add_paragraph(texto)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


# ── 2.1 — analizar expands zip, persists members, returns proposals ─────────


async def test_analizar_expande_zip_persiste_miembros_y_propone(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch)
    _patch_llm(monkeypatch)
    zip_bytes = _build_zip({"informe.docx": _build_docx_bytes(), "cuenta.docx": _build_docx_bytes("Cuenta de cobro")})

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:analizar",
        headers=test_user["headers"],
        files={"file": ("plantillas.zip", zip_bytes, "application/zip")},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert {p["filename"] for p in data["propuestas"]} == {"informe.docx", "cuenta.docx"}
    assert all(p["documento_fuente_id"] for p in data["propuestas"])

    result = await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))
    persistidos = result.scalars().all()
    assert len(persistidos) == 2
    assert {d.tipo for d in persistidos} == {TipoDocumentoFuente.PLANTILLA}


# ── 2.2 — cap overflow → truncation aviso ────────────────────────────────────


async def test_analizar_archivo_que_excede_cap_de_miembros_avisa_truncamiento(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.agent.tools.document_parser._ARCHIVE_MAX_MEMBERS", 2)
    _patch_storage(monkeypatch)
    _patch_llm(monkeypatch)
    zip_bytes = _build_zip({f"m{i}.docx": _build_docx_bytes(f"texto {i}") for i in range(3)})

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:analizar",
        headers=test_user["headers"],
        files={"file": ("plantillas.zip", zip_bytes, "application/zip")},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["propuestas"]) <= 2
    assert any("límite" in a for a in data["avisos"])


# ── 2.3 — .rar without unrar/bsdtar → aviso, upload still 200 ───────────────


async def test_analizar_rar_sin_backend_avisa_y_no_falla(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(svc.shutil, "which", lambda _tool: None)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:analizar",
        headers=test_user["headers"],
        files={"file": ("plantillas.rar", b"not-a-real-rar", "application/x-rar-compressed")},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["propuestas"] == []
    assert any("rar" in a.lower() for a in data["avisos"])


# ── 2.4 — empty archive → aviso, no ingestion ────────────────────────────────


async def test_analizar_archivo_vacio_avisa_y_no_ingiere(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch)
    _patch_llm(monkeypatch)
    zip_bytes = _build_zip({})

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:analizar",
        headers=test_user["headers"],
        files={"file": ("vacio.zip", zip_bytes, "application/zip")},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["propuestas"] == []
    assert any("vacío" in a for a in data["avisos"])
    result = await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))
    assert result.scalars().all() == []


# ── 2.5 — duplicate tipo_documento in confirm batch → validation error ──────


async def test_confirmar_rechaza_tipo_documento_duplicado(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    doc_a = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k1",
        nombre="a.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    doc_b = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k2",
        nombre="b.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    db.add_all([doc_a, doc_b])
    await db.commit()
    await db.refresh(doc_a)
    await db.refresh(doc_b)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:confirmar",
        headers=test_user["headers"],
        json={
            "miembros": [
                {"documento_fuente_id": str(doc_a.id), "tipo_documento": "cuenta_cobro"},
                {"documento_fuente_id": str(doc_b.id), "tipo_documento": "cuenta_cobro"},
            ]
        },
    )

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert str(doc_a.id) in detail
    assert str(doc_b.id) in detail


# ── gap fix — confirmed tipo_documento wins over extension-derived one ──────


async def test_confirmar_persiste_tipo_documento_confirmado_no_el_derivado_de_extension(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ingerir_plantilla_organismo` derives cuenta_cobro/documento_soporte from
    the file extension (.xlsx vs not) for tipo=PLANTILLA docs — the member here
    is `.docx`, which extension-derives to "cuenta_cobro", but the user
    confirmed "documento_soporte". The confirmed choice must win."""
    _patch_llm(monkeypatch)
    user = test_user["user"]
    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k-mismatch",
        nombre="soporte.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    storage = _InMemoryStorage()
    storage.store["k-mismatch"] = _build_docx_bytes()
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_kw: storage)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:confirmar",
        headers=test_user["headers"],
        json={"miembros": [{"documento_fuente_id": str(doc.id), "tipo_documento": "documento_soporte"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["resultados"][0]["persistida"] is True

    result = await db.execute(select(PlantillaOrganismo).where(PlantillaOrganismo.usuario_id == user.id))
    plantilla = result.scalar_one()
    assert plantilla.tipo_documento == "documento_soporte"


# ── gap fix 2 — confirmed-tipo override collision: newest wins, no IntegrityError ──


async def test_confirmar_tipo_override_colisiona_con_plantilla_existente_newest_wins(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PRE-EXISTING PlantillaOrganismo already occupies (usuario, entidad,
    "documento_soporte"). Confirming a `.docx` member (extension-derives to
    "cuenta_cobro") AS "documento_soporte" makes the override collide with
    that existing row on the unique constraint — newest wins: the old row is
    superseded, never a raw IntegrityError."""
    _patch_llm(monkeypatch)
    user = test_user["user"]
    vieja = PlantillaOrganismo(
        usuario_id=user.id,
        entidad="DAGMA",
        entidad_normalizada="dagma",
        tipo_documento="documento_soporte",
        formato="xlsx",
        estructura_json={"campos": [], "clonable": False, "avisos": ["vieja"]},
    )
    db.add(vieja)
    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k-colision",
        nombre="nueva.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    storage = _InMemoryStorage()
    storage.store["k-colision"] = _build_docx_bytes()
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_kw: storage)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:confirmar",
        headers=test_user["headers"],
        json={"miembros": [{"documento_fuente_id": str(doc.id), "tipo_documento": "documento_soporte"}]},
    )

    assert resp.status_code == 200
    resultado = resp.json()["resultados"][0]
    assert resultado["persistida"] is True
    assert resultado["avisos"] == [] or "IntegrityError" not in str(resultado["avisos"])

    result = await db.execute(
        select(PlantillaOrganismo).where(
            PlantillaOrganismo.usuario_id == user.id,
            PlantillaOrganismo.entidad_normalizada == "dagma",
            PlantillaOrganismo.tipo_documento == "documento_soporte",
        )
    )
    filas = result.scalars().all()
    assert len(filas) == 1
    superviviente = filas[0]
    assert superviviente.fuente_documento_id == doc.id
    assert superviviente.formato == "docx"


# ── 2.6 — cancel before confirm → no PlantillaOrganismo created/updated ─────


async def test_analizar_sin_confirmar_no_crea_plantilla_organismo(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch)
    _patch_llm(monkeypatch)
    zip_bytes = _build_zip({"informe.docx": _build_docx_bytes()})

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:analizar",
        headers=test_user["headers"],
        files={"file": ("plantillas.zip", zip_bytes, "application/zip")},
    )
    assert resp.status_code == 200

    result = await db.execute(select(PlantillaOrganismo).where(PlantillaOrganismo.usuario_id == test_user["user"].id))
    assert result.scalars().all() == []


# ── 2.7 — 10 confirmed members → Semaphore(3) bounds concurrent ingestion ──


async def test_ingerir_miembros_bounded_limita_concurrencia_a_tres(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = test_user["user"]
    docs = []
    for i in range(10):
        doc = DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            storage_key=f"k{i}",
            nombre=f"m{i}.docx",
            tipo=TipoDocumentoFuente.PLANTILLA,
        )
        db.add(doc)
        docs.append(doc)
    await db.commit()
    for d in docs:
        await db.refresh(d)

    contador = {"actual": 0, "maximo": 0}
    lock = asyncio.Lock()

    async def _fake_ingerir(
        member_db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID, documento_fuente_id
    ):
        async with lock:
            contador["actual"] += 1
            contador["maximo"] = max(contador["maximo"], contador["actual"])
        await asyncio.sleep(0.02)
        async with lock:
            contador["actual"] -= 1
        return None, []

    monkeypatch.setattr(svc, "ingerir_plantilla_organismo", _fake_ingerir)

    session_factory = async_sessionmaker(bind=db.bind, class_=AsyncSession, expire_on_commit=False)
    miembros = [MiembroConfirmado(documento_fuente_id=d.id, tipo_documento="cuenta_cobro") for d in docs]

    resultados = await svc._ingerir_miembros_bounded(session_factory, user.id, contrato.id, miembros)

    assert len(resultados) == 10
    assert contador["maximo"] == 3


# ── 2.8 — mixed success/failure batch → aggregated avisos ───────────────────


async def test_confirmar_lote_mixto_agrega_avisos_sin_fallar_todo(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch)
    _patch_llm(monkeypatch)
    user = test_user["user"]
    doc_ok = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k-ok",
        nombre="ok.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    db.add(doc_ok)
    await db.commit()
    await db.refresh(doc_ok)
    doc_ok_storage = _InMemoryStorage()
    monkeypatch.setattr("app.services.document_service._get_storage", lambda *_a, **_kw: doc_ok_storage)
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_kw: doc_ok_storage)
    doc_ok_storage.store["k-ok"] = _build_docx_bytes()

    faltante_id = uuid.uuid4()

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:confirmar",
        headers=test_user["headers"],
        json={
            "miembros": [
                {"documento_fuente_id": str(doc_ok.id), "tipo_documento": "informe_actividades"},
                {"documento_fuente_id": str(faltante_id), "tipo_documento": "cuenta_cobro"},
            ]
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["resultados"]) == 2
    persistidas = {r["documento_fuente_id"]: r["persistida"] for r in data["resultados"]}
    assert persistidas[str(doc_ok.id)] is True
    assert persistidas[str(faltante_id)] is False


# ── RISK-001 — oversized archivo:analizar upload → 4xx, not unbounded memory ──


async def test_analizar_rechaza_archivo_mas_grande_que_max_file_size_bytes(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Every other upload endpoint (`documentos.py:108`) enforces
    `validate_file_size`/`MAX_FILE_SIZE_BYTES` — `archivo:analizar` must too."""
    oversized = b"PK\x03\x04" + b"0" * (MAX_FILE_SIZE_BYTES + 1)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:analizar",
        headers=test_user["headers"],
        files={"file": ("plantillas.zip", oversized, "application/zip")},
    )

    assert 400 <= resp.status_code < 500


# ── REL-001 — concurrent IntegrityError on the intermediate derived key ─────


async def test_confirmar_recupera_de_integrity_error_concurrente_con_retry(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulates the REL-001 race: under `Semaphore(3)`, two same-derived-key
    members can both attempt an INSERT before either commits; the loser's
    flush raises a raw `IntegrityError`. `_ingerir_uno` must catch it,
    rollback, and retry once — by then the "winner"'s row is committed and
    the retry resolves via the UPDATE branch instead of crashing the batch."""
    _patch_llm(monkeypatch)
    user = test_user["user"]
    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k-race",
        nombre="a.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    storage = _InMemoryStorage()
    storage.store["k-race"] = _build_docx_bytes()
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_kw: storage)

    llamadas = {"n": 0}
    original = svc.ingerir_plantilla_organismo

    async def _fake_ingerir(member_db, usuario_id, contrato_id, documento_fuente_id):  # type: ignore[no-untyped-def]
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise IntegrityError("INSERT", {}, Exception("UNIQUE constraint failed"))
        return await original(member_db, usuario_id, contrato_id, documento_fuente_id)

    monkeypatch.setattr(svc, "ingerir_plantilla_organismo", _fake_ingerir)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:confirmar",
        headers=test_user["headers"],
        json={"miembros": [{"documento_fuente_id": str(doc.id), "tipo_documento": "cuenta_cobro"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["resultados"][0]["persistida"] is True
    assert llamadas["n"] == 2


# ── RES-001 — LLM classification failure still uploads eligible members ────


async def test_analizar_con_llm_caido_igual_sube_miembros_para_pick_manual(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch)

    class _FailingLLM:
        async def complete(self, *_a: Any, **_kw: Any) -> LLMResponse:
            raise RuntimeError("llm down")

    monkeypatch.setattr("app.adapters.llm.get_llm", lambda model=None: _FailingLLM(), raising=True)
    zip_bytes = _build_zip({"informe.docx": _build_docx_bytes()})

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:analizar",
        headers=test_user["headers"],
        files={"file": ("plantillas.zip", zip_bytes, "application/zip")},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["propuestas"]) == 1
    assert data["propuestas"][0]["tipo_propuesto"] is None
    assert any("clasific" in a.lower() for a in data["avisos"])
    result = await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))
    assert len(result.scalars().all()) == 1


# ── RES-002 — one member's storage failure doesn't abort the whole batch ───


async def test_confirmar_lote_con_fallo_de_storage_no_aborta_el_batch(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_llm(monkeypatch)
    user = test_user["user"]
    doc_ok = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k-ok2",
        nombre="ok.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    doc_fail = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="k-missing",
        nombre="fail.docx",
        tipo=TipoDocumentoFuente.PLANTILLA,
    )
    db.add_all([doc_ok, doc_fail])
    await db.commit()
    await db.refresh(doc_ok)
    await db.refresh(doc_fail)
    storage = _InMemoryStorage()
    storage.store["k-ok2"] = _build_docx_bytes()
    # "k-missing" intentionally absent — storage.download raises FileNotFoundError.
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_kw: storage)

    resp = await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/archivo:confirmar",
        headers=test_user["headers"],
        json={
            "miembros": [
                {"documento_fuente_id": str(doc_ok.id), "tipo_documento": "cuenta_cobro"},
                {"documento_fuente_id": str(doc_fail.id), "tipo_documento": "documento_soporte"},
            ]
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    persistidas = {r["documento_fuente_id"]: r["persistida"] for r in data["resultados"]}
    assert persistidas[str(doc_ok.id)] is True
    assert persistidas[str(doc_fail.id)] is False
    assert data["avisos"]
