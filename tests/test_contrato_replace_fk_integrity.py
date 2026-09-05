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
from app.core.exceptions import ValidationError
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


async def _referenciar_primera_obligacion(
    db: AsyncSession, contrato_id: uuid.UUID, estado: EstadoCuentaCobro
) -> tuple[Obligacion, Actividad]:
    """Attach an Actividad + EvidenciaObligacion to the contract's first obligación."""
    obligaciones = await _obligaciones(db, contrato_id)
    assert obligaciones, "precondition: v1 must have produced obligations"
    obligacion = obligaciones[0]

    cuenta = await _crear_cuenta(db, contrato_id, estado)
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


class TestActiveCuentaBlocksReplace:
    async def test_enviada_cuenta_blocks_replace_and_leaves_previous_document_intact(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """An `enviada` cuenta referencing an obligación must reject the replace
        BEFORE anything (storage object or DB row) is touched."""
        user_id = test_user["user"].id
        contrato = await _crear_contrato(db, user_id, numero="CD-FK-002")
        contrato_id = contrato.id

        await _subir_contrato(db, user_id, contrato_id, "contrato-v1.txt", _TEXTO_V1)
        await _referenciar_primera_obligacion(db, contrato_id, EstadoCuentaCobro.ENVIADA)
        obligaciones_antes = {o.descripcion for o in await _obligaciones(db, contrato_id)}

        storage = _mock_storage()
        with pytest.raises(ValidationError) as exc_info:
            await _subir_contrato(db, user_id, contrato_id, "contrato-v2.txt", _TEXTO_V2, storage=storage)

        assert "enviada" in str(exc_info.value.detail).lower()
        storage.delete.assert_not_called()
        storage.upload.assert_not_called()

        await db.rollback()
        db.expunge_all()  # rollback expires the identity map; force a fresh read
        nombres = (await db.execute(_nombres_documentos(contrato_id))).scalars().all()
        assert list(nombres) == ["contrato-v1.txt"], f"the old document must be intact: {nombres}"
        assert {o.descripcion for o in await _obligaciones(db, contrato_id)} == obligaciones_antes
