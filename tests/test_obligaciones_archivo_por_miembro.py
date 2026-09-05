"""B6: obligation extraction from an uploaded ARCHIVE must use only the member
that belongs to the selected contract — never the concatenation of every member.

`_asegurar_texto_extraido_manual` -> `extraer_texto_documento` ->
`document_parser.parse_archive` concatenates ALL members of a `.zip` (200k
cap), and obligations were then extracted from that concatenation. A zip
containing the selected contract's clausulado AND a foreign contract's
document (e.g. a certificado de experiencia bundle) leaked the foreign
contract's obligations onto the selected one.
"""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion
from app.services.document_service import upload_document
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"

_NUMERO_SELECCIONADO = "4112.020.26.1.482-2026"
_NUMERO_AJENO = "4146.010.26.1.101-2024"

_TEXTO_CONTRATO_SELECCIONADO = f"""
CONTRATO No. {_NUMERO_SELECCIONADO} - SECRETARIA DE DESARROLLO ECONOMICO
CLÁUSULA SEGUNDA. OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:
1. Elaborar los estudios previos de los procesos de contratacion.
2. Revisar los actos administrativos que expida la Secretaria.
3. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.
"""

_TEXTO_CONTRATO_AJENO = f"""
CONTRATO No. {_NUMERO_AJENO} - SECRETARIA DE GESTION DE RIESGO
OBLIGACIONES DEL CONTRATISTA:
A) Dar cumplimiento a las actividades establecidas en el plan de trabajo de la Secretaria de Gestion de Riesgo.
B) Prestar servicio profesional de apoyo en los tramites juridicos de la Secretaria de Gestion de Riesgo.
"""

_ENTIDAD_AJENA = "gestion de riesgo"


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_contrato(db: AsyncSession, user_id: uuid.UUID, numero: str) -> Contrato:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Prestacion de servicios profesionales",
        valor_total=22_162_000.0,
        valor_mensual=2_216_200.0,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return contrato


async def _obligaciones(db: AsyncSession, contrato_id: uuid.UUID) -> list[Obligacion]:
    res = await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato_id))
    return list(res.scalars().all())


async def _subir_zip(db: AsyncSession, user_id: uuid.UUID, contrato_id: uuid.UUID, content: bytes, filename: str):
    with patch(_PATCH_S3) as mock_storage_cls:
        mock_storage_cls.return_value = _mock_storage()
        return await upload_document(
            db=db,
            user_id=user_id,
            filename=filename,
            content=content,
            content_type="application/zip",
            contrato_id=contrato_id,
            requisito_codigo="CONTRATO",
        )


class TestArchiveObligationsUseOnlyTheMatchingMember:
    async def test_foreign_member_never_contaminates_obligations(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id, _NUMERO_SELECCIONADO)
        content = _make_zip(
            {
                "contrato-seleccionado.txt": _TEXTO_CONTRATO_SELECCIONADO.encode(),
                "certificado-ajeno.txt": _TEXTO_CONTRATO_AJENO.encode(),
            }
        )

        await _subir_zip(db, user.id, contrato.id, content, "soportes.zip")

        obligaciones = await _obligaciones(db, contrato.id)
        ajenas = [o.descripcion for o in obligaciones if _ENTIDAD_AJENA in o.descripcion.lower()]
        assert not ajenas, f"the foreign member's obligations leaked onto the contract: {ajenas}"
        assert any("estudios previos" in o.descripcion.lower() for o in obligaciones), (
            "the selected contract's own obligations must still be extracted"
        )

    async def test_no_matching_member_extracts_nothing_and_warns(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """No member mentions the selected contract's número — never fall back
        to the concatenation of unrelated members."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id, _NUMERO_SELECCIONADO)
        content = _make_zip(
            {
                "certificado-ajeno-1.txt": _TEXTO_CONTRATO_AJENO.encode(),
                "certificado-ajeno-2.txt": _TEXTO_CONTRATO_AJENO.replace(_NUMERO_AJENO, "9999.010.1.1-2020").encode(),
            }
        )

        result = await _subir_zip(db, user.id, contrato.id, content, "soportes.zip")

        obligaciones = await _obligaciones(db, contrato.id)
        assert obligaciones == [], f"no member matched the contract; nothing should be extracted: {obligaciones}"
        assert any("archivo comprimido" in a.lower() for a in result.avisos), (
            f"expected an aviso about no matching archive member, got: {result.avisos}"
        )


class TestEmptyArchiveIsExplained:
    async def test_archive_with_no_readable_members_warns_instead_of_failing_silently(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """An archive whose members yield no text extracted nothing AND said
        nothing — the user saw a successful upload with zero obligations and no
        explanation."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id, _NUMERO_SELECCIONADO)
        # A .png member is on the never-read-as-text list, so the archive yields
        # no members with usable text at all.
        content = _make_zip({"captura.png": bytes([0x89]) + b"PNG" + b"0" * 64})

        result = await _subir_zip(db, user.id, contrato.id, content, "soportes.zip")

        assert await _obligaciones(db, contrato.id) == []
        assert any("archivo comprimido" in a.lower() for a in result.avisos), (
            f"expected an aviso about the unreadable archive, got: {result.avisos}"
        )
