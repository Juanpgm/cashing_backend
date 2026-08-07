"""LLM read-and-decide disambiguation over a CONTRATO borderline tie
(secop-contrato-disambiguation-auto-obligaciones, PR-1b, design D2).

`detectar_desde_secop` calls `_desambiguar_contrato_base` only for the
CONTRATO requisito, only when there are >= 2 candidates whose leading
(filename-ranked) score sits in the borderline band
[CONTENIDO_SCORE_AMBIGUO, AUTO_LINK_THRESHOLD_CONTRATO). It reads up to 3
candidates' text (native first, vision only on native failure) and asks the
LLM which one is the base contrato/clausulado, or none. Bounded by
`_DISAMBIGUACION_CONTRATO_TIMEOUT_SEGUNDOS`; ANY failure degrades silently to
the unchanged filename-ranked top[0] (never blocks the scan).
"""

from __future__ import annotations

import asyncio
import io
import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.categoria_documento import CategoriaDocumento
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    EstadoRequisito,
)
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.secop import SecopDocumento
from app.schemas.agent import ContratoBaseVeredicto, LLMResponse
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_GET_LLM = "app.adapters.llm.get_llm"

# Deliberately keyword-neutral (no CATEGORIA_KEYWORDS hits for ANY category:
# no "contrato"/"minuta"/"clausulado"/"prestacion de servicios"/etc.) so the
# borderline content-sniff (`_sniff_requisito`, runs before disambiguation for
# CONTRATO/RPC/CDP) never reclassifies/rescales these docs above their
# filename-derived score — these tests target the disambiguation step itself,
# not the content-sniff.
TEXTO_A = "Documento identificador ALFA. Información administrativa de referencia sin más detalle."
TEXTO_B = "Documento identificador BETA. Información administrativa de referencia sin más detalle."
TEXTO_C = "Documento identificador GAMMA. Información administrativa de referencia sin más detalle."


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-DISAMB-001",
        objeto="Servicios de disambiguacion",
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


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int = 1) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    await checklist_service.asegurar_checklist(db, cuenta=cc)
    await db.commit()
    return cc


def _secop_doc(contrato: Contrato, nombre: str, **kwargs: Any) -> SecopDocumento:
    return SecopDocumento(
        id_documento_secop=f"DOC-{uuid.uuid4().hex[:10]}",
        numero_contrato=contrato.numero_contrato,
        nombre_archivo=nombre,
        datos_raw={},
        **kwargs,
    )


def _docx_bytes(texto: str) -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph(texto)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def descargas(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    payloads: dict[str, bytes] = {}

    async def _fake(url: str, timeout: float = checklist_service._SNIFF_HTTP_TIMEOUT) -> bytes:
        return payloads[url]

    monkeypatch.setattr(checklist_service, "_descargar_secop_bytes", _fake)
    return {"payloads": payloads}


@pytest.fixture
def extraccion(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Spy on the obligaciones-extraction seam; persists one Obligacion like the real fn."""
    from app.services import document_service

    llamadas: list[tuple[str, uuid.UUID]] = []

    async def _fake(texto: str, contrato_id: uuid.UUID | None, db: AsyncSession):
        llamadas.append((texto, contrato_id))
        db.add(
            Obligacion(
                contrato_id=contrato_id,
                descripcion="Obligación extraída del contrato SECOP",
                tipo=TipoObligacion.GENERAL,
                orden=1,
            )
        )
        await db.flush()
        return [], []

    monkeypatch.setattr(document_service, "extraer_obligaciones_texto", _fake)
    return {"llamadas": llamadas}


async def _fila_contrato(db: AsyncSession, cuenta_id: uuid.UUID) -> DocumentoCuentaCobro:
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id,
            DocumentoCuentaCobro.requisito_codigo == "CONTRATO",
        )
    )
    return res.scalar_one()


async def _candidatos_contrato(db: AsyncSession, cuenta_id: uuid.UUID) -> list[DocumentoChecklistCandidato]:
    res = await db.execute(
        select(DocumentoChecklistCandidato)
        .where(
            DocumentoChecklistCandidato.cuenta_cobro_id == cuenta_id,
            DocumentoChecklistCandidato.requisito_codigo == "CONTRATO",
        )
        .order_by(DocumentoChecklistCandidato.score.desc())
    )
    return list(res.scalars().all())


def _tres_candidatos_en_banda(contrato: Contrato, descargas: dict[str, Any]) -> tuple[SecopDocumento, ...]:
    """3 CONTRATO SECOP docs whose LEADING (doc_a) filename score sits in the
    borderline band [0.600, 0.75) — a plausible tie the LLM must break.
    doc_b/doc_c deliberately score lower still (not just "close ties") so a
    "none"/error/timeout degrade — which only caps/keeps top[0] in place,
    never re-sorts the rest — still leaves top[0] as the highest-scored
    candidate for the score.desc() assertions below."""
    especificaciones = [
        ("contrato_a.docx", "https://s/a.docx", TEXTO_A, 0.680),
        ("contrato_b.docx", "https://s/b.docx", TEXTO_B, 0.550),
        ("contrato_c.docx", "https://s/c.docx", TEXTO_C, 0.520),
    ]
    docs = []
    for nombre, url, texto, confianza in especificaciones:
        descargas["payloads"][url] = _docx_bytes(texto)
        docs.append(
            _secop_doc(
                contrato,
                nombre,
                categoria=CategoriaDocumento.CONTRATO,
                categoria_confianza=confianza,
                url_descarga=url,
            )
        )
    return tuple(docs)


def _mock_llm(resp: LLMResponse | None = None, side_effect: Exception | None = None) -> AsyncMock:
    mock_llm = AsyncMock()
    if side_effect is not None:
        mock_llm.complete = AsyncMock(side_effect=side_effect)
    else:
        mock_llm.complete = AsyncMock(return_value=resp)
    return mock_llm


async def test_llm_elige_confiado_autolinca_y_autoextrae(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """3 candidates in the borderline band, LLM confidently picks index 1
    (contrato_b) → it becomes top[0] with confianza=0.900 (>= 0.75) → the
    existing PR-1a gate silently auto-extracts obligaciones from ITS text."""
    doc_a, doc_b, doc_c = _tres_candidatos_en_banda(contrato, descargas)
    db.add_all([doc_a, doc_b, doc_c])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    veredicto = ContratoBaseVeredicto(elegido_index=1, confianza=0.9, razon="clausulado con obligaciones")
    mock_llm = _mock_llm(resp=LLMResponse(content=veredicto.model_dump_json(), model="test"))

    with patch(_PATCH_GET_LLM, return_value=mock_llm) as mock_get_llm:
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    mock_get_llm.assert_called()
    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc_b.id
    assert fila.confianza_deteccion == Decimal("0.900")
    assert len(extraccion["llamadas"]) == 1
    texto_usado, contrato_id = extraccion["llamadas"][0]
    assert "BETA" in texto_usado
    assert contrato_id == contrato.id

    candidatos = await _candidatos_contrato(db, cuenta.id)
    top = next(c for c in candidatos if c.secop_documento_id == doc_b.id)
    assert top.score_origen == "llm"


async def test_llm_dice_ninguno_deja_banda_baja_sin_autoextraer(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """LLM verdict "none" caps top[0] below 0.700 (AUTO_LINK_THRESHOLD): the
    fila is NOT auto-linked at all (not even the shared 0.700 bar), obligaciones
    are never silently extracted, and the panel path (manual Vincular) stands."""
    doc_a, doc_b, doc_c = _tres_candidatos_en_banda(contrato, descargas)
    db.add_all([doc_a, doc_b, doc_c])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    veredicto = ContratoBaseVeredicto(elegido_index=None, confianza=0.1, razon="ninguno es el contrato base")
    mock_llm = _mock_llm(resp=LLMResponse(content=veredicto.model_dump_json(), model="test"))

    with patch(_PATCH_GET_LLM, return_value=mock_llm) as mock_get_llm:
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    mock_get_llm.assert_called()
    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.secop_documento_id is None
    assert extraccion["llamadas"] == []

    candidatos = await _candidatos_contrato(db, cuenta.id)
    assert candidatos[0].score == Decimal("0.600")


async def _llm_lenta(*args: Any, **kwargs: Any) -> LLMResponse:
    veredicto = ContratoBaseVeredicto(elegido_index=1, confianza=0.9, razon="tarde")
    await asyncio.sleep(0.2)
    return LLMResponse(content=veredicto.model_dump_json(), model="test")


@pytest.mark.parametrize(
    "mock_llm",
    [_mock_llm(side_effect=RuntimeError("llm caído")), AsyncMock(complete=_llm_lenta)],
    ids=["llm_error", "llm_timeout"],
)
async def test_llm_falla_degrada_silenciosamente_a_top_filename(
    db: AsyncSession,
    contrato: Contrato,
    descargas: dict[str, Any],
    extraccion: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    mock_llm: AsyncMock,
) -> None:
    """An LLM error OR a call past the deadline must never break the scan:
    both degrade to the UNCHANGED filename-ranked top (not even capped —
    that's the "none" verdict, a different outcome) and the request
    completes normally."""
    monkeypatch.setattr(checklist_service, "_DISAMBIGUACION_CONTRATO_TIMEOUT_SEGUNDOS", 0.01)
    doc_a, _doc_b, _doc_c = _tres_candidatos_en_banda(contrato, descargas)
    db.add_all([doc_a, _doc_b, _doc_c])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    with patch(_PATCH_GET_LLM, return_value=mock_llm):
        resultado = await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    assert "CONTRATO" in resultado  # scan completed, no exception surfaced
    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.PENDIENTE  # 0.680 < AUTO_LINK_THRESHOLD (0.700), unchanged
    assert extraccion["llamadas"] == []
    candidatos = await _candidatos_contrato(db, cuenta.id)
    assert candidatos[0].secop_documento_id == doc_a.id
    assert candidatos[0].score == Decimal("0.680")  # untouched, not even capped


async def test_un_solo_candidato_no_invoca_llm(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """A lone CONTRATO candidate (no tie to break) skips the LLM call entirely
    — no wasted round-trip."""
    url = "https://s/unico.docx"
    descargas["payloads"][url] = _docx_bytes(TEXTO_A)
    doc = _secop_doc(
        contrato,
        "contrato_unico.docx",
        categoria=CategoriaDocumento.CONTRATO,
        categoria_confianza=0.65,
        url_descarga=url,
    )
    db.add(doc)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    with patch(_PATCH_GET_LLM) as mock_get_llm:
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    mock_get_llm.assert_not_called()
    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.PENDIENTE  # 0.650 < 0.700, unaffected either way
    assert extraccion["llamadas"] == []


async def test_llm_confianza_fuera_de_rango_se_clampa_a_uno(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """RELIABILITY-001: a hallucinated out-of-[0,1] confianza (here 15.0) must be
    clamped to 1.000 before it reaches the Numeric(4,3) score columns — no
    commit-time overflow, and the picked doc auto-links/auto-extracts at 1.000
    (not some spurious 15.0)."""
    doc_a, doc_b, doc_c = _tres_candidatos_en_banda(contrato, descargas)
    db.add_all([doc_a, doc_b, doc_c])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    veredicto = ContratoBaseVeredicto(elegido_index=1, confianza=15.0, razon="alucinado")
    mock_llm = _mock_llm(resp=LLMResponse(content=veredicto.model_dump_json(), model="test"))

    with patch(_PATCH_GET_LLM, return_value=mock_llm):
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()  # would raise on Numeric(4,3) overflow if unclamped

    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc_b.id
    assert fila.confianza_deteccion == Decimal("1.000")
    assert len(extraccion["llamadas"]) == 1


async def test_llm_indice_fuera_de_rango_se_trata_como_ninguno(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """RELIABILITY-002: an out-of-range elegido_index (5, for 3 candidates) hits
    the same guard as a null verdict — top[0] capped to 0.600, no auto-link, no
    silent extraction."""
    doc_a, doc_b, doc_c = _tres_candidatos_en_banda(contrato, descargas)
    db.add_all([doc_a, doc_b, doc_c])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    veredicto = ContratoBaseVeredicto(elegido_index=5, confianza=0.9, razon="indice imposible")
    mock_llm = _mock_llm(resp=LLMResponse(content=veredicto.model_dump_json(), model="test"))

    with patch(_PATCH_GET_LLM, return_value=mock_llm):
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.secop_documento_id is None
    assert extraccion["llamadas"] == []
    candidatos = await _candidatos_contrato(db, cuenta.id)
    assert candidatos[0].score == Decimal("0.600")


async def test_menos_de_dos_candidatos_con_texto_degrada_sin_llm(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """When fewer than 2 of the tie's candidates yield usable text (here only
    doc_a downloads; doc_b/doc_c fail both native and vision), the LLM is never
    called and top degrades unchanged — the `len(candidatos) < 2` early return."""
    doc_a, doc_b, doc_c = _tres_candidatos_en_banda(contrato, descargas)
    # Drop b/c payloads so their native AND vision downloads both raise → None.
    del descargas["payloads"]["https://s/b.docx"]
    del descargas["payloads"]["https://s/c.docx"]
    db.add_all([doc_a, doc_b, doc_c])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    with patch(_PATCH_GET_LLM) as mock_get_llm:
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    mock_get_llm.assert_not_called()
    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.PENDIENTE  # 0.680 < 0.700, unchanged
    assert extraccion["llamadas"] == []
    candidatos = await _candidatos_contrato(db, cuenta.id)
    assert candidatos[0].score == Decimal("0.680")  # untouched


async def test_nativo_falla_escala_a_vision_y_desambigua(
    db: AsyncSession,
    contrato: Contrato,
    descargas: dict[str, Any],
    extraccion: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vision-escalation branch of `_texto_candidato_contrato` (closes verify
    WARNING): when native extraction yields no usable text (scanned candidate),
    the disambiguation escalates to the multimodal/vision reader, and the LLM
    decides over the vision-transcribed text."""
    from types import SimpleNamespace

    from app.services import document_service

    # Native pass yields nothing (doc stays text-less) → force the vision branch.
    async def _sin_texto_nativo(doc: SecopDocumento, presupuesto: Any) -> None:
        return None

    vision_llamadas: list[str] = []

    async def _fake_vision(data: bytes, mime: str) -> Any:
        vision_llamadas.append(mime)
        return SimpleNamespace(transcripcion=f"VISION-TRANSCRIPCION len={len(data)}")

    monkeypatch.setattr(checklist_service, "_asegurar_texto_extraido", _sin_texto_nativo)
    monkeypatch.setattr(document_service, "_extraer_contrato_multimodal", _fake_vision)

    doc_a, doc_b, doc_c = _tres_candidatos_en_banda(contrato, descargas)
    db.add_all([doc_a, doc_b, doc_c])
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    veredicto = ContratoBaseVeredicto(elegido_index=0, confianza=0.9, razon="clausulado escaneado")
    mock_llm = _mock_llm(resp=LLMResponse(content=veredicto.model_dump_json(), model="test"))

    with patch(_PATCH_GET_LLM, return_value=mock_llm) as mock_get_llm:
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    # Core of this test: native yielded nothing → the vision reader WAS invoked
    # (once per candidate), and the LLM decided over that vision-transcribed text,
    # landing the confident pick as the detected CONTRATO. (Downstream obligaciones
    # auto-extraction is covered by the other tests; the global native no-op here
    # deliberately isolates the read-escalation branch.)
    assert vision_llamadas  # native failed → vision was actually invoked
    mock_get_llm.assert_called()
    fila = await _fila_contrato(db, cuenta.id)
    assert fila.estado == EstadoRequisito.DETECTADO
    assert fila.secop_documento_id == doc_a.id  # the LLM-picked (index 0) candidate


async def test_rpc_cdp_nunca_invoca_desambiguacion_contrato(
    db: AsyncSession, contrato: Contrato, descargas: dict[str, Any], extraccion: dict[str, Any]
) -> None:
    """Regression: an RPC (Registro Presupuestal) borderline tie — the same
    shape of ambiguity the CONTRATO branch resolves via LLM — must NEVER
    invoke the CONTRATO disambiguation step. RPC/CDP scoring is untouched."""
    especificaciones = [
        ("rpc_a.pdf", "https://s/rpc_a.pdf", 0.680),
        ("rpc_b.pdf", "https://s/rpc_b.pdf", 0.660),
    ]
    docs = []
    for nombre, url, confianza in especificaciones:
        descargas["payloads"][url] = _docx_bytes("Registro Presupuestal del Compromiso No. 55")
        docs.append(
            _secop_doc(
                contrato,
                nombre,
                categoria=CategoriaDocumento.REGISTRO_PRESUPUESTAL,
                categoria_confianza=confianza,
                url_descarga=url,
            )
        )
    db.add_all(docs)
    await db.commit()
    cuenta = await _make_cuenta(db, contrato)

    with patch(_PATCH_GET_LLM) as mock_get_llm:
        await checklist_service.detectar_desde_secop(db, cuenta)
        await db.commit()

    mock_get_llm.assert_not_called()
    assert extraccion["llamadas"] == []
