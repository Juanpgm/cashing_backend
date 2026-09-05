"""RED regression harness for the 2026-09-04 "wrong obligations" production report.

Reported symptoms (radicar stepper, contract 4112.020.26.1.482-2026):
  S2 — uploading the "complemento del contrato" PDF (whose CLÁUSULA with
       "OBLIGACIONES ESPECÍFICAS" enumerates 5 items) extracted a SINGLE
       catch-all activity, "Todas aquellas inherentes para el cumplimiento
       del objeto contractual".
  S3 — obligations shown come from OTHER contracts quoted inside an uploaded
       "certificado de experiencia" PDF, not from the selected contract.
  S4 — Paso 2 "OBLIGACIONES POR EVIDENCIAR" lists obligations (markers A, B,
       C, D) that belong to a different entity's contract.

These tests assert the CORRECT behaviour, so they are RED on production code.
They are diagnosis artifacts: left unstaged, no fix applied.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.agent.tools.contract_parser import extract_obligaciones_verbatim
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion
from app.services.document_service import upload_document
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"

# ── Fixtures: realistic Colombian public-contract text ────────────────────────

# The "complemento del contrato": a preamble PARÁGRAFO merely *mentions*
# "obligaciones específicas" and closes with a catch-all bullet; the REAL
# enumerated list lives in the next clause.
COMPLEMENTO_TEXTO = """
CLAUSULAS COMPLEMENTARIAS AL CONTRATO DE PRESTACION DE SERVICIOS No. 4112.020.26.1.482-2026
SANTIAGO DE CALI DISTRITO ESPECIAL - SECRETARIA DE DESARROLLO ECONOMICO

CLÁUSULA PRIMERA. OBJETO: Prestacion de servicios profesionales de apoyo juridico.
PARAGRAFO: Las obligaciones especificas del contratista se desarrollan mas adelante y comprenden:
- Todas aquellas inherentes para el cumplimiento del objeto contractual.

CLÁUSULA SEGUNDA. ALCANCE DEL OBJETO CONTRACTUAL Y OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:
1. Elaborar los estudios previos de los procesos de contratacion de la dependencia.
2. Revisar los actos administrativos que expida la Secretaria de Desarrollo Economico.
3. Acompanar juridicamente las audiencias publicas de adjudicacion.
4. Rendir los informes mensuales de ejecucion del contrato.
5. Todas aquellas inherentes para el cumplimiento del objeto contractual.

CLÁUSULA TERCERA. VALOR DEL CONTRATO: VEINTIDOS MILLONES CIENTO SESENTA Y DOS MIL PESOS.
"""

# A "certificado de experiencia" that quotes OTHER contracts and their
# obligations. It is not the selected contract's clausulado at all.
EXPERIENCIA_TEXTO = """
CERTIFICADO DE EXPERIENCIA

La suscrita certifica que la contratista ejecuto los siguientes contratos:

CONTRATO No. 4146.010.26.1.101-2024 - ALCALDIA DE SANTIAGO DE CALI - SECRETARIA DE GESTION DE RIESGO
OBLIGACIONES DEL CONTRATISTA:
A) Dar cumplimiento a las actividades establecidas en el plan de trabajo de la Secretaria de Gestion de Riesgo.
B) Prestar servicio profesional de apoyo en los tramites juridicos de la Secretaria de Gestion de Riesgo.
C) Proyectar respuestas a los derechos de peticion de la Alcaldia de Santiago de Cali - Secretaria de Gestion de Riesgo.
D) Participar en el comite de estructuracion de los procesos de la dependencia.

CONTRATO No. 4131.010.26.1.055-2023 - DAGMA
OBLIGACIONES DEL CONTRATISTA:
1. Apoyar la revision tecnica de los expedientes ambientales del DAGMA.
"""

_ENTIDAD_AJENA = "gestion de riesgo"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_contrato(db: AsyncSession, user_id: uuid.UUID, numero: str) -> Contrato:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Prestacion de servicios profesionales de apoyo juridico",
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
    res = await db.execute(
        select(Obligacion).where(Obligacion.contrato_id == contrato_id).order_by(Obligacion.orden)
    )
    return list(res.scalars().all())


async def _subir(
    db: AsyncSession,
    user_id: uuid.UUID,
    contrato_id: uuid.UUID,
    filename: str,
    texto: str,
    *,
    requisito_codigo: str | None = None,
) -> None:
    """Drive the Paso 1 upload shape: tipo=contrato, contrato_id.

    ``requisito_codigo`` defaults to None — the exact shape of a direct/agent
    caller that passes `tipo=CONTRATO` without explicitly declaring contract
    scope (B3: this must NOT be treated as the contract). Pass
    `requisito_codigo="CONTRATO"` explicitly to drive the genuine contract-
    replace path instead (mirrors the frontend's `buildContratoUploadParams`,
    which sends it whenever `tipo === "contrato"`).
    """
    with patch(_PATCH_S3) as mock_storage_cls:
        mock_storage_cls.return_value = _mock_storage()
        await upload_document(
            db=db,
            user_id=user_id,
            filename=filename,
            content=texto.encode("utf-8"),
            content_type="text/plain",
            tipo=TipoDocumentoFuente.CONTRATO,
            contrato_id=contrato_id,
            requisito_codigo=requisito_codigo,
        )


# ── S2: the complement's real specific-obligations list is skipped ────────────


class TestComplementoExtraeSoloElCatchAll:
    def test_verbatim_returns_the_five_specific_obligations(self) -> None:
        """S2: only the catch-all comes back; the real 5-item list is ignored."""
        obligaciones = extract_obligaciones_verbatim(COMPLEMENTO_TEXTO)
        descripciones = [o.descripcion for o in obligaciones]

        assert len(obligaciones) == 5, (
            f"expected the 5 enumerated obligations of CLÁUSULA SEGUNDA, got {descripciones}"
        )
        assert any("estudios previos" in d for d in descripciones)
        assert any("informes mensuales" in d for d in descripciones)


# ── S3/S4: a non-contract document writes obligations onto the contract ───────


class TestDocumentoAjenoContaminaObligaciones:
    async def test_certificado_experiencia_no_escribe_obligaciones_del_contrato(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """S3/S4: an experience certificate must not seed the contract's obligations."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id, "4112.020.26.1.482-2026")

        await _subir(db, user.id, contrato.id, "certificado-experiencia.txt", EXPERIENCIA_TEXTO)

        obligaciones = await _obligaciones(db, contrato.id)
        ajenas = [o.descripcion for o in obligaciones if _ENTIDAD_AJENA in o.descripcion.lower()]
        assert not ajenas, (
            "obligations from another entity's contract were written onto the selected "
            f"contract: {ajenas}"
        )

    async def test_reemplazar_documento_contrato_limpia_obligaciones_previas(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """S3: the 1-doc-per-contract replace rule leaves the old doc's obligations behind.

        Upload the complement, then upload the experience certificate (which the
        replace rule treats as the new single contract document). The obligations
        extracted from the *replaced* document must not survive alongside the new ones.
        """
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id, "4112.020.26.1.482-2026")

        # Both uploads explicitly declare contract scope (matches the frontend's
        # `buildContratoUploadParams`) so the replace rule actually fires — this
        # test exercises B4 (clear-on-replace), not B3 (scope guard).
        await _subir(
            db, user.id, contrato.id, "complemento-contrato.txt", COMPLEMENTO_TEXTO, requisito_codigo="CONTRATO"
        )
        antes = {o.descripcion for o in await _obligaciones(db, contrato.id)}
        assert antes, "precondition: the complement must have produced obligations"

        await _subir(
            db, user.id, contrato.id, "certificado-experiencia.txt", EXPERIENCIA_TEXTO, requisito_codigo="CONTRATO"
        )

        docs = (
            await db.execute(
                select(DocumentoFuente).where(
                    DocumentoFuente.contrato_id == contrato.id,
                    DocumentoFuente.tipo == TipoDocumentoFuente.CONTRATO,
                )
            )
        ).scalars().all()
        assert len(docs) == 1, "replace rule keeps a single contract document"

        despues = {o.descripcion for o in await _obligaciones(db, contrato.id)}
        huerfanas = antes & despues
        assert not huerfanas, (
            "obligations extracted from the REPLACED document survived the replacement: "
            f"{sorted(huerfanas)}"
        )

    async def test_obligaciones_no_cruzan_entre_contratos_del_mismo_usuario(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Control: two contracts of the same user must not share obligations.

        Expected GREEN — pins that the leak is at WRITE time (which document feeds
        the extractor), not a missing contrato_id filter at READ time.
        """
        user = test_user["user"]
        contrato_a = await _crear_contrato(db, user.id, "4112.020.26.1.482-2026")
        contrato_b = await _crear_contrato(db, user.id, "4146.010.26.1.101-2024")

        await _subir(db, user.id, contrato_a.id, "complemento-a.txt", COMPLEMENTO_TEXTO)
        await _subir(db, user.id, contrato_b.id, "experiencia-b.txt", EXPERIENCIA_TEXTO)

        de_a = {o.descripcion for o in await _obligaciones(db, contrato_a.id)}
        de_b = {o.descripcion for o in await _obligaciones(db, contrato_b.id)}
        assert not (de_a & de_b), "obligations bled across contracts of the same user"


class TestExtraerDocumentoEligeElDocumentoEquivocado:
    async def test_extraer_obligaciones_documento_ignora_documentos_de_cuenta(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """Latent H1 variant: the extract endpoint picks ANY tipo=CONTRATO row.

        ``document_service.extraer_obligaciones_documento`` selects the contract
        document with ``.limit(1)`` and no ORDER BY and no
        ``cuenta_cobro_id IS NULL`` filter, so a cuenta-scoped attachment that
        happens to carry ``tipo=contrato`` can win over the real clausulado.
        """
        from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
        from app.services.document_service import extraer_obligaciones_documento

        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id, "4112.020.26.1.482-2026")
        cuenta = CuentaCobro(
            contrato_id=contrato.id,
            mes=1,
            anio=2026,
            estado=EstadoCuentaCobro.BORRADOR,
            valor=2_216_200,
            requisitos_modo="estandar",
        )
        db.add(cuenta)
        await db.commit()
        await db.refresh(cuenta)

        # A cuenta-scoped attachment that kept tipo=contrato (inserted first, so
        # an unordered LIMIT 1 reaches it).
        db.add(
            DocumentoFuente(
                usuario_id=user.id,
                contrato_id=contrato.id,
                cuenta_cobro_id=cuenta.id,
                storage_key="usuarios/x/documentos/adjunto/experiencia.pdf",
                nombre="certificado-experiencia.pdf",
                tipo=TipoDocumentoFuente.CONTRATO,
                texto_extraido=EXPERIENCIA_TEXTO,
            )
        )
        await db.commit()

        # The real contract clausulado, contract-level.
        await _subir(db, user.id, contrato.id, "complemento-contrato.txt", COMPLEMENTO_TEXTO)

        (await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato.id)))  # touch
        from sqlalchemy import delete as _delete

        await db.execute(_delete(Obligacion).where(Obligacion.contrato_id == contrato.id))
        await db.commit()

        await extraer_obligaciones_documento(contrato.id, user.id, db)

        ajenas = [
            o.descripcion
            for o in await _obligaciones(db, contrato.id)
            if _ENTIDAD_AJENA in o.descripcion.lower()
        ]
        assert not ajenas, f"extraction read a cuenta-scoped attachment, not the contract: {ajenas}"
