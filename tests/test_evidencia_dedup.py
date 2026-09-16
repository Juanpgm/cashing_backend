"""Tests for evidencia upload dedup by content hash (Req 10a, migración 038).

Re-uploading identical bytes must not create a new row or storage object, and
a commit failure right after an upload must not leave an orphaned storage
object behind. Mirrors fixtures/conventions from tests/test_evidencia.py.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.services import evidencia_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_CONTENIDO = b"contenido identico de evidencia para dedup"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload.return_value = "evidencias/test/key.pdf"
    storage.presigned_url.return_value = "https://s3.example.com/presigned"
    storage.delete.return_value = None
    return storage


async def _crear_contrato(db: AsyncSession, usuario_id: Any, numero: str) -> Contrato:
    c = Contrato(
        usuario_id=usuario_id,
        numero_contrato=numero,
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


async def _crear_cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
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
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    return await _crear_contrato(db, test_user["user"].id, "CTR-DEDUP-001")


@pytest.fixture
async def cuenta_cobro(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    return await _crear_cuenta(db, contrato)


async def test_reupload_misma_cuenta_no_duplica(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(a) Re-uploading identical bytes to the same cuenta → 1 row + 1 storage
    object; the second response marks itself as duplicate."""
    user = test_user["user"]
    storage = _mock_storage()

    r1 = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("doc.txt", "text/plain", _CONTENIDO)],
    )
    r2 = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("doc-otra-vez.txt", "text/plain", _CONTENIDO)],
    )

    assert r1[0].duplicada is False
    assert r2[0].duplicada is True
    assert r2[0].id == r1[0].id
    assert storage.upload.call_count == 1

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 1


async def test_misma_bytes_otra_cuenta_crea_fila_nueva(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(b) Same bytes uploaded to a DIFFERENT cuenta → a new row IS created."""
    user = test_user["user"]
    storage = _mock_storage()

    otro_contrato = await _crear_contrato(db, user.id, "CTR-DEDUP-002")
    otra_cuenta = await _crear_cuenta(db, otro_contrato)

    await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[("doc.txt", "text/plain", _CONTENIDO)],
    )
    r2 = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=otra_cuenta.id,
        archivos=[("doc.txt", "text/plain", _CONTENIDO)],
    )

    assert r2[0].duplicada is False
    assert storage.upload.call_count == 2

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 2


async def test_dos_archivos_identicos_en_un_lote_se_guardan_una_vez(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(d) Two identical files in ONE upload batch → stored once."""
    user = test_user["user"]
    storage = _mock_storage()

    resultados = await evidencia_service.subir_evidencias_cuenta(
        db=db,
        storage=storage,
        usuario_id=user.id,
        cuenta_id=cuenta_cobro.id,
        archivos=[
            ("copia1.txt", "text/plain", _CONTENIDO),
            ("copia2.txt", "text/plain", _CONTENIDO),
        ],
    )

    assert resultados[0].duplicada is False
    assert resultados[1].duplicada is True
    assert resultados[1].id == resultados[0].id
    assert storage.upload.call_count == 1

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 1


async def test_commit_fallido_borra_objeto_huerfano_en_storage(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(c) A commit failure right after the storage upload must trigger a
    best-effort `storage.delete` for the just-uploaded object and leave no
    Evidencia row behind — never an orphaned S3-compatible object."""
    user = test_user["user"]
    storage = _mock_storage()

    with (
        patch.object(db, "commit", AsyncMock(side_effect=RuntimeError("boom"))),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("doc.txt", "text/plain", _CONTENIDO)],
        )

    storage.upload.assert_called_once()
    uploaded_key = storage.upload.call_args.kwargs["key"]
    storage.delete.assert_called_once_with(key=uploaded_key)

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert rows == []


async def test_batch_dedup_una_sola_query_in_para_todo_el_lote(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(radicacion-sin-friccion 2.6) Uploading N files that ALL already exist
    (from a prior, separate request) must issue exactly ONE batched
    `sha256 IN (...)` dedup query, not one SELECT per file."""
    from tests.conftest import QueryCounter, engine_test

    user = test_user["user"]
    storage = _mock_storage()

    # Seed 3 pre-existing evidencias (separate prior requests).
    previos = [b"contenido-a-batch-dedup", b"contenido-b-batch-dedup", b"contenido-c-batch-dedup"]
    for contenido in previos:
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[("previo.txt", "text/plain", contenido)],
        )

    counter = QueryCounter(engine_test.sync_engine)
    counter.start()
    try:
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                ("re-a.txt", "text/plain", previos[0]),
                ("re-b.txt", "text/plain", previos[1]),
                ("re-c.txt", "text/plain", previos[2]),
            ],
        )
    finally:
        counter.stop()

    assert all(r.duplicada for r in resultados)
    # Filter on the WHERE-clause shape of the batched dedup query specifically
    # (`evidencias.sha256 IN (...)`) — every SELECT against `evidencias` also
    # lists the `sha256` COLUMN in its SELECT clause, so a bare "sha256"
    # substring match would over-count.
    dedup_selects = [s for s in counter.statements if "evidencias.sha256 IN" in s]
    assert len(dedup_selects) == 1, (
        f"expected exactly 1 batched dedup SELECT, got {len(dedup_selects)}: {dedup_selects}"
    )


async def test_lote_de_archivos_distintos_hace_un_solo_commit(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """(radicacion-sin-friccion 2.6) N genuinely distinct files in one batch
    must be persisted with exactly ONE `db.commit()` call for the upload
    itself — spied via a wrapped `db.commit`, not inferred from end state.
    The inline classification step that runs after upload (`_clasificar_y_
    enlazar_lote`, no obligaciones here so it degrades to
    `marcar_evidencia_sin_obligacion` calls, each with its own commit) is
    stubbed out so this test isolates the upload-batching commit count."""
    user = test_user["user"]
    storage = _mock_storage()

    original_commit = db.commit
    commit_spy = AsyncMock(wraps=original_commit)

    with (
        patch.object(evidencia_service, "_clasificar_y_enlazar_lote", AsyncMock(return_value=None)),
        patch.object(db, "commit", commit_spy),
    ):
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                ("d1.txt", "text/plain", b"contenido distinto uno"),
                ("d2.txt", "text/plain", b"contenido distinto dos"),
                ("d3.txt", "text/plain", b"contenido distinto tres"),
            ],
        )

    assert len(resultados) == 3
    assert all(not r.duplicada for r in resultados)
    assert storage.upload.call_count == 3
    assert commit_spy.call_count == 1

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 3


async def test_lote_con_duplicado_intra_lote_hace_un_solo_commit(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """Same as (d) above, plus the commit-count assertion: two identical files
    in ONE batch → ONE upload, ONE commit for the whole batch (the in-memory
    seen-set resolves the second file WITHOUT a second DB round trip)."""
    user = test_user["user"]
    storage = _mock_storage()

    original_commit = db.commit
    commit_spy = AsyncMock(wraps=original_commit)

    with (
        patch.object(evidencia_service, "_clasificar_y_enlazar_lote", AsyncMock(return_value=None)),
        patch.object(db, "commit", commit_spy),
    ):
        resultados = await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_cobro.id,
            archivos=[
                ("copia1.txt", "text/plain", _CONTENIDO),
                ("copia2.txt", "text/plain", _CONTENIDO),
            ],
        )

    assert resultados[0].duplicada is False
    assert resultados[1].duplicada is True
    assert resultados[1].id == resultados[0].id
    assert storage.upload.call_count == 1
    assert commit_spy.call_count == 1

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert len(rows) == 1


async def test_falla_upload_a_mitad_de_lote_limpia_los_que_si_subieron(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """Partial-batch-failure design decision: if one file's storage upload
    fails mid-batch, the WHOLE batch fails atomically — every OTHER file's
    already-succeeded upload in this same batch is best-effort deleted from
    storage (never orphaned) and NO Evidencia row is persisted for any file
    in the batch, including the ones that uploaded fine.

    Also asserts the load-bearing invariant a review found untested
    (radicacion-sin-friccion slice 2.6 review): the planning pass that runs
    BEFORE the upload phase can `db.flush()` a new Actividad stub row
    (`_find_or_create_actividad_stub`) that was never committed — a failure
    here must not leave that flushed-but-uncommitted stub pending either."""
    user = test_user["user"]
    storage = _mock_storage()
    # Plain value, captured BEFORE the call: the service's own db.rollback()
    # on the upload-failure path expires every ORM object tracked by `db`,
    # including this fixture — touching `cuenta_cobro.id` again afterward
    # would trigger an implicit lazy-reload outside an awaited context
    # (MissingGreenlet). Same idiom already established elsewhere in this
    # test suite for the identical reason.
    cuenta_id = cuenta_cobro.id

    async def _upload_side_effect(*, key: str, data: bytes, content_type: str):
        if "falla" in key:
            raise RuntimeError("storage boom")
        return key

    storage.upload = AsyncMock(side_effect=_upload_side_effect)

    with pytest.raises(RuntimeError, match="storage boom"):
        await evidencia_service.subir_evidencias_cuenta(
            db=db,
            storage=storage,
            usuario_id=user.id,
            cuenta_id=cuenta_id,
            archivos=[
                ("ok1.txt", "text/plain", b"contenido ok uno"),
                ("falla.txt", "text/plain", b"contenido que falla"),
                ("ok2.txt", "text/plain", b"contenido ok dos"),
            ],
        )

    # The two files that DID succeed at storage.upload must be cleaned up —
    # storage.delete called once per successfully-uploaded key.
    assert storage.delete.call_count == 2

    rows = (await db.execute(select(Evidencia))).scalars().all()
    assert rows == []

    from app.models.actividad import Actividad

    stub_rows = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))).scalars().all()
    assert stub_rows == [], (
        "a flushed-but-uncommitted Actividad stub from the planning pass "
        "leaked past the failed batch — subir_evidencias_cuenta must roll "
        "back its own session on an upload failure, not rely on the caller"
    )


async def test_subir_evidencia_actividad_reupload_no_duplica(
    db: AsyncSession, test_user: dict[str, Any], cuenta_cobro: CuentaCobro
) -> None:
    """Same dedup contract on the per-actividad single-file upload site
    (`subir_evidencia`) — same actividad, same bytes, second call is marked
    duplicate and no second row/storage object is created."""
    from app.models.actividad import Actividad

    user = test_user["user"]
    storage = _mock_storage()
    a = Actividad(cuenta_cobro_id=cuenta_cobro.id, descripcion="Actividad dedup", fecha_realizacion=date(2024, 3, 15))
    db.add(a)
    await db.commit()
    await db.refresh(a)

    r1 = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=a.id,
        filename="informe.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.4 contenido evidencia",
    )
    r2 = await evidencia_service.subir_evidencia(
        db=db,
        storage=storage,
        usuario_id=user.id,
        actividad_id=a.id,
        filename="informe-otra-vez.pdf",
        content_type="application/pdf",
        data=b"%PDF-1.4 contenido evidencia",
    )

    assert r1.duplicada is False
    assert r2.duplicada is True
    assert r2.id == r1.id
    assert storage.upload.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Round 2 — id is AUTHORITATIVE identity (confirmed CRITICAL finding).
#
# `_deduplicate` dropped an item whose content hash collided EVEN WHEN its
# external id was new, so: recurring Calendar events (content excludes the
# date, so instances are byte-identical), same-named Drive files (content IS
# the filename) and short identical email bodies ("Recibido, gracias.") all
# collapsed to one. Dedup runs BEFORE filter/matcher, so the loss is upstream
# of everything.
# ─────────────────────────────────────────────────────────────────────────────


def test_dedup_keeps_recurring_calendar_events_with_distinct_event_ids():
    from app.agent.nodes.evidence_dedup import _deduplicate

    events = [{"source": "calendar", "event_id": f"e{i}", "content": "Comite de seguimiento"} for i in range(4)]
    kept = _deduplicate(events)
    assert len(kept) == 4, "recurring weekly meeting instances collapsed into one"


def test_dedup_keeps_same_named_drive_files_with_distinct_file_ids():
    from app.agent.nodes.evidence_dedup import _deduplicate

    files = [
        {"source": "drive", "file_id": f"f{i}", "title": "Informe mensual.pdf", "content": "Informe mensual.pdf"}
        for i in range(3)
    ]
    kept = _deduplicate(files)
    assert len(kept) == 3, "monthly documents sharing a filename collapsed into one"


def test_dedup_keeps_distinct_emails_with_identical_short_bodies():
    from app.agent.nodes.evidence_dedup import _deduplicate

    emails = [
        {"source": "email", "message_id": "m1", "title": "Re: Informe enero", "content": "Recibido, gracias."},
        {"source": "email", "message_id": "m2", "title": "Re: Informe febrero", "content": "Recibido, gracias."},
        {"source": "email", "message_id": "m3", "title": "Re: Acta de reunion", "content": "Recibido, gracias."},
    ]
    kept = _deduplicate(emails)
    assert len(kept) == 3
    assert [e["message_id"] for e in kept] == ["m1", "m2", "m3"]


def test_dedup_still_collapses_the_same_id_refetched():
    """The id remains a real dedup signal — this must not regress."""
    from app.agent.nodes.evidence_dedup import _deduplicate

    items = [
        {"source": "email", "message_id": "m1", "content": "cuerpo original"},
        {"source": "email", "message_id": "m1", "content": "cuerpo con snippet actualizado"},
    ]
    assert len(_deduplicate(items)) == 1


def test_dedup_still_collapses_idless_items_with_identical_content():
    """Content hashing is retained ONLY for items with no external id."""
    from app.agent.nodes.evidence_dedup import _deduplicate

    items = [
        {"source": "local", "content": "mismo texto exacto"},
        {"source": "local", "content": "mismo texto exacto"},
    ]
    assert len(_deduplicate(items)) == 1


def test_dedup_keeps_idless_items_with_empty_content():
    """Root cause #6 must stay fixed: `_content_hash("")` is a constant."""
    from app.agent.nodes.evidence_dedup import _deduplicate

    items = [{"source": "local", "content": ""}, {"source": "local", "content": ""}]
    assert len(_deduplicate(items)) == 2


def test_dedup_cross_provider_same_file_collapses_on_name_size_mime():
    """The docstring's cross-provider case (same document, different provider
    file ids) is the one thing id-authoritative dedup would lose — keyed on
    (name, size, mime) instead of the bare filename so two genuinely different
    files that merely share a name are NOT collapsed."""
    from app.agent.nodes.evidence_dedup import _deduplicate

    items = [
        {
            "source": "drive",
            "file_id": "g1",
            "provider": "google",
            "title": "Informe mensual.pdf",
            "content": "Informe mensual.pdf",
            "size": 12345,
            "mime_type": "application/pdf",
        },
        {
            "source": "drive",
            "file_id": "m1",
            "provider": "microsoft",
            "title": "Informe mensual.pdf",
            "content": "Informe mensual.pdf",
            "size": 12345,
            "mime_type": "application/pdf",
        },
    ]
    assert len(_deduplicate(items)) == 1

    # Same name, DIFFERENT size → two distinct documents, both kept.
    items[1]["size"] = 999
    assert len(_deduplicate(items)) == 2
