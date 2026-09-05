"""BLOCKER: replacing the contract document wiped obligations with a raw
``delete(Obligacion)``.

Neither ``actividades.obligacion_id`` (app/models/actividad.py) nor
``evidencia_obligacion.obligacion_id`` (app/models/evidencia_obligacion.py)
declares an ``ondelete``, so on Postgres that bulk delete raises
``ForeignKeyViolation`` → 500. By then the PREVIOUS document's storage object had
already been deleted best-effort (``contextlib.suppress(Exception)``) outside any
transaction, so the contract PDF was gone for good while the DB rolled back.

SQLite test runs hid this: the in-memory test database runs with
``PRAGMA foreign_keys=OFF``, so the raw delete "succeeded" and merely left
dangling references. The first test here turns FK enforcement ON for its own
connection so the violation is actually observable.

The fix routes the wipe through ``contrato_service.limpiar_obligaciones``, which
(a) guards against cuentas in an active state (enviada/aprobada/pagada) BEFORE
touching anything, (b) nulls ``actividades.obligacion_id``, (c) deletes the
``evidencia_obligacion`` link rows, and only then (d) deletes the obligaciones.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion
from app.services.document_service import upload_document
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"

_IS_SQLITE = not os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:").startswith("postgresql")

# 200+ chars so is_text_sufficient() passes without OCR/vision mocks, and a real
# tier-1 obligations section so the deterministic verbatim extractor (no LLM) hits.
_TEXTO_BASE = (
    "CLÁUSULA SEGUNDA. OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "1. Elaborar los estudios previos {marca} de los procesos de contratacion.\n"
    "2. Revisar los actos administrativos {marca} que expida la entidad.\n"
    "3. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
)
_TEXTO_V1 = _TEXTO_BASE.format(marca="V1")
_TEXTO_V2 = _TEXTO_BASE.format(marca="V2")

# Reconcile fixtures (BLOCKER B, round-2 review obs #492): a replace must match the
# new extraction against the existing obligations by normalized text and only touch
# the ones that genuinely differ, instead of wiping and reinserting everything.
_TEXTO_TRES = (
    "CLÁUSULA SEGUNDA. OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "1. Elaborar los estudios previos de los procesos de contratacion.\n"
    "2. Revisar los actos administrativos que expida la entidad.\n"
    "3. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
)
# Same three obligations, item 2 dropped.
_TEXTO_DOS = (
    "CLÁUSULA SEGUNDA. OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "1. Elaborar los estudios previos de los procesos de contratacion.\n"
    "2. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
)
# Same three obligations plus a new one (inserted before the catch-all, which must
# stay last — see `_split_items`'s "stop after the catch-all" rule).
_TEXTO_CUATRO = (
    "CLÁUSULA SEGUNDA. OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "1. Elaborar los estudios previos de los procesos de contratacion.\n"
    "2. Revisar los actos administrativos que expida la entidad.\n"
    "3. Actualizar el inventario de bienes asignados para el desarrollo del contrato.\n"
    "4. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
)
# Same three obligations, differing only by case/accents/whitespace — must
# normalize to the SAME key as `_TEXTO_TRES` (`app.core.text_match.normalize`).
_TEXTO_TRES_VARIANTE = (
    "CLÁUSULA SEGUNDA. OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "1.   ELABORAR   los  Estudios Previos de los procesos de contratación.\n"
    "2. revisar   LOS actos administrativos que expida la entidad.\n"
    "3. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
)


@pytest.fixture
async def sqlite_fk_enforcement(db: AsyncSession) -> AsyncGenerator[None, None]:
    """Enforce SQLite foreign keys for THIS test's connection only.

    Scoped deliberately: the global conftest keeps the default (``OFF``) so the
    rest of the suite is unaffected. On Postgres FKs are always enforced, so this
    is a no-op there.
    """
    if not _IS_SQLITE:
        yield
        return
    await db.execute(text("PRAGMA foreign_keys=ON"))
    try:
        yield
    finally:
        await db.rollback()
        await db.execute(text("PRAGMA foreign_keys=OFF"))
        await db.commit()


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_contrato(db: AsyncSession, user_id: uuid.UUID, numero: str = "CD-FK-001") -> Contrato:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Objeto de prueba",
        valor_total=12_000_000.0,
        valor_mensual=1_000_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return contrato


async def _crear_cuenta(db: AsyncSession, contrato_id: uuid.UUID, estado: EstadoCuentaCobro) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato_id,
        mes=1,
        anio=2025,
        estado=estado,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)
    return cuenta


async def _obligaciones(db: AsyncSession, contrato_id: uuid.UUID) -> list[Obligacion]:
    res = await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato_id))
    return list(res.scalars().all())


def _nombres_documentos(contrato_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Column-only select — survives an expunged identity map after a rollback."""
    return select(DocumentoFuente.nombre).where(DocumentoFuente.contrato_id == contrato_id)


async def _subir_contrato(
    db: AsyncSession,
    user_id: uuid.UUID,
    contrato_id: uuid.UUID,
    filename: str,
    texto: str,
    storage: AsyncMock | None = None,
) -> Any:
    with patch(_PATCH_S3) as mock_storage_cls:
        mock_storage_cls.return_value = storage if storage is not None else _mock_storage()
        return await upload_document(
            db=db,
            user_id=user_id,
            filename=filename,
            content=texto.encode("utf-8"),
            content_type="text/plain",
            contrato_id=contrato_id,
            requisito_codigo="CONTRATO",
        )


async def _referenciar_obligacion(
    db: AsyncSession, obligacion: Obligacion, estado: EstadoCuentaCobro
) -> Actividad:
    """Attach an Actividad + EvidenciaObligacion to the given obligación."""
    cuenta = await _crear_cuenta(db, obligacion.contrato_id, estado)
    actividad = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad que referencia la obligación",
    )
    db.add(actividad)
    await db.commit()
    await db.refresh(actividad)

    evidencia = Evidencia(actividad_id=actividad.id, nombre_archivo="soporte.pdf")
    db.add(evidencia)
    await db.commit()
    await db.refresh(evidencia)

    db.add(EvidenciaObligacion(evidencia_id=evidencia.id, obligacion_id=obligacion.id))
    await db.commit()
    return actividad


async def _referenciar_primera_obligacion(
    db: AsyncSession, contrato_id: uuid.UUID, estado: EstadoCuentaCobro
) -> tuple[Obligacion, Actividad]:
    """Attach an Actividad + EvidenciaObligacion to the contract's first obligación."""
    obligaciones = await _obligaciones(db, contrato_id)
    assert obligaciones, "precondition: v1 must have produced obligations"
    obligacion = obligaciones[0]
    actividad = await _referenciar_obligacion(db, obligacion, estado)
    return obligacion, actividad


class TestReplaceRespectsForeignKeys:
    async def test_replace_with_referenced_obligacion_does_not_violate_foreign_keys(
        self, db: AsyncSession, test_user: dict[str, Any], sqlite_fk_enforcement: None
    ) -> None:
        """A draft cuenta's actividad + evidencia link must not block the replace,
        and must not leave the bulk delete violating their FKs either."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)

        await _subir_contrato(db, user.id, contrato.id, "contrato-v1.txt", _TEXTO_V1)
        obligacion, actividad = await _referenciar_primera_obligacion(
            db, contrato.id, EstadoCuentaCobro.BORRADOR
        )

        await _subir_contrato(db, user.id, contrato.id, "contrato-v2.txt", _TEXTO_V2)

        descripciones = {o.descripcion for o in await _obligaciones(db, contrato.id)}
        assert any("V2" in d for d in descripciones), f"v2's obligations must be present: {descripciones}"
        assert not any("V1" in d for d in descripciones), f"v1's obligations must be gone: {descripciones}"

        await db.refresh(actividad)
        assert actividad.obligacion_id is None, "the actividad must survive with its obligación reference nulled"

        enlaces = (
            await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.obligacion_id == obligacion.id))
        ).scalars().all()
        assert enlaces == [], "evidencia_obligacion rows pointing at deleted obligaciones must be removed"


class TestActiveCuentaAcceptsReplaceButKeepsObligaciones:
    """MAJOR 4 (round-2 review obs #492): an active cuenta must not block the
    document replace itself — only the destructive reconcile is skipped. The old
    behaviour (422 for the whole upload) meant a contractor could never fix a
    wrong/outdated contract file once any cuenta moved past BORRADOR, and — worse
    — only fired when the new document was actually readable, so an unreadable
    replacement was silently accepted while a good one was rejected."""

    async def test_enviada_cuenta_replaces_the_document_and_keeps_obligaciones_untouched(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user_id = test_user["user"].id
        contrato = await _crear_contrato(db, user_id, numero="CD-FK-002")
        contrato_id = contrato.id

        await _subir_contrato(db, user_id, contrato_id, "contrato-v1.txt", _TEXTO_V1)
        obligacion, actividad = await _referenciar_primera_obligacion(db, contrato_id, EstadoCuentaCobro.ENVIADA)
        ids_antes = {o.id: o.descripcion for o in await _obligaciones(db, contrato_id)}

        storage = _mock_storage()
        resultado = await _subir_contrato(db, user_id, contrato_id, "contrato-v2.txt", _TEXTO_V2, storage=storage)

        assert any(
            "cuentas de cobro enviadas" in a and "conservaron las obligaciones existentes" in a
            for a in resultado.avisos
        ), f"expected the active-cuenta aviso, got: {resultado.avisos}"

        nombres = (await db.execute(_nombres_documentos(contrato_id))).scalars().all()
        assert list(nombres) == ["contrato-v2.txt"], f"the document must be replaced: {nombres}"
        storage.upload.assert_awaited_once()

        ids_despues = {o.id: o.descripcion for o in await _obligaciones(db, contrato_id)}
        assert ids_despues == ids_antes, f"obligaciones (and their ids) must be untouched: {ids_despues}"

        await db.refresh(actividad)
        assert actividad.obligacion_id == obligacion.id, "the actividad's link must survive untouched"


class TestReconcileObligacionesOnReplace:
    """BLOCKER B (round-2 review obs #492): a replace must reconcile the new
    extraction against the existing obligaciones by normalized text instead of
    wiping and reinserting everything — preserving ids (and every actividad/
    evidencia link built on them) for obligaciones that are still present."""

    async def test_identical_reupload_keeps_every_id_and_every_link(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """(a) The exact same content re-uploaded (double-click, retry after a
        timeout) must not touch a single obligación or link."""
        user_id = test_user["user"].id
        contrato = await _crear_contrato(db, user_id, numero="CD-RECONCILE-A")
        contrato_id = contrato.id

        await _subir_contrato(db, user_id, contrato_id, "contrato.txt", _TEXTO_TRES)
        obligacion, actividad = await _referenciar_primera_obligacion(db, contrato_id, EstadoCuentaCobro.BORRADOR)
        ids_antes = {o.id: o.descripcion for o in await _obligaciones(db, contrato_id)}

        resultado = await _subir_contrato(db, user_id, contrato_id, "contrato.txt", _TEXTO_TRES)

        ids_despues = {o.id: o.descripcion for o in await _obligaciones(db, contrato_id)}
        assert ids_despues == ids_antes, f"identical re-upload must not change a single id: {ids_despues}"
        assert not any("eliminaron" in a for a in resultado.avisos), resultado.avisos

        await db.refresh(actividad)
        assert actividad.obligacion_id == obligacion.id, "the actividad's link must survive"
        enlaces = (
            await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.obligacion_id == obligacion.id))
        ).scalars().all()
        assert len(enlaces) == 1, "the evidencia_obligacion row must survive"

    async def test_dropped_obligacion_is_removed_fk_safely_the_rest_keep_their_ids(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """(b) The new document drops item 2 of 3: only item 2 is removed
        (FK-safely — its actividad/evidencia links are cleared, not orphaned),
        items 1 and 3 keep their ids untouched."""
        user_id = test_user["user"].id
        contrato = await _crear_contrato(db, user_id, numero="CD-RECONCILE-B")
        contrato_id = contrato.id

        await _subir_contrato(db, user_id, contrato_id, "contrato.txt", _TEXTO_TRES)
        obligaciones_antes = await _obligaciones(db, contrato_id)
        assert len(obligaciones_antes) == 3, obligaciones_antes
        por_desc = {o.descripcion: o for o in obligaciones_antes}
        item2 = next(o for o in obligaciones_antes if "actos administrativos" in o.descripcion)
        actividad = await _referenciar_obligacion(db, item2, EstadoCuentaCobro.BORRADOR)

        resultado = await _subir_contrato(db, user_id, contrato_id, "contrato-v2.txt", _TEXTO_DOS)

        assert any("Se eliminaron 1 obligaciones" in a for a in resultado.avisos), resultado.avisos

        despues = await _obligaciones(db, contrato_id)
        assert len(despues) == 2, [o.descripcion for o in despues]
        ids_despues = {o.descripcion: o.id for o in despues}
        item1 = "Elaborar los estudios previos de los procesos de contratacion"
        item3_catch_all = (
            "Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato"
        )
        assert ids_despues[item1] == por_desc[item1].id
        assert ids_despues[item3_catch_all] == por_desc[item3_catch_all].id
        assert not any(d.startswith("Revisar los actos administrativos") for d in ids_despues), ids_despues

        await db.refresh(actividad)
        assert actividad.obligacion_id is None, "the dropped obligación's actividad link must be nulled"
        enlaces = (
            await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.obligacion_id == item2.id))
        ).scalars().all()
        assert enlaces == [], "the dropped obligación's evidencia links must be removed"

    async def test_added_obligacion_keeps_the_three_original_ids_and_appends_a_fourth(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """(c) The new document adds a 4th obligación: the original 3 keep their
        ids, the new one is appended."""
        user_id = test_user["user"].id
        contrato = await _crear_contrato(db, user_id, numero="CD-RECONCILE-C")
        contrato_id = contrato.id

        await _subir_contrato(db, user_id, contrato_id, "contrato.txt", _TEXTO_TRES)
        ids_antes = {o.descripcion: o.id for o in await _obligaciones(db, contrato_id)}
        assert len(ids_antes) == 3, ids_antes

        await _subir_contrato(db, user_id, contrato_id, "contrato-v2.txt", _TEXTO_CUATRO)

        despues = await _obligaciones(db, contrato_id)
        assert len(despues) == 4, [o.descripcion for o in despues]
        for descripcion, id_ in ids_antes.items():
            coincidencia = next(o for o in despues if o.descripcion == descripcion)
            assert coincidencia.id == id_, f"'{descripcion}' must keep its original id"
        assert any("inventario de bienes" in o.descripcion for o in despues), despues

    async def test_case_accent_and_whitespace_only_differences_are_treated_as_the_same_text(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """(e) A re-extraction that differs only by case/accents/whitespace must
        normalize to the same key (`app.core.text_match.normalize`) and therefore
        change nothing."""
        user_id = test_user["user"].id
        contrato = await _crear_contrato(db, user_id, numero="CD-RECONCILE-E")
        contrato_id = contrato.id

        await _subir_contrato(db, user_id, contrato_id, "contrato.txt", _TEXTO_TRES)
        ids_antes = {o.descripcion: o.id for o in await _obligaciones(db, contrato_id)}
        assert len(ids_antes) == 3, ids_antes

        resultado = await _subir_contrato(db, user_id, contrato_id, "contrato-v2.txt", _TEXTO_TRES_VARIANTE)

        ids_despues = {o.descripcion: o.id for o in await _obligaciones(db, contrato_id)}
        assert len(ids_despues) == 3, ids_despues
        assert set(ids_despues.values()) == set(ids_antes.values()), (
            f"a whitespace/case/accent-only difference must not touch any id: {ids_despues} vs {ids_antes}"
        )
        assert not any("eliminaron" in a for a in resultado.avisos), resultado.avisos
