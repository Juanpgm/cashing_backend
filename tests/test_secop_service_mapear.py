"""Tests for secop_service pure helper functions: _calcular_valor_mensual, _mapear_a_contrato_create."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest

# U+FFFD is always written as an escape, never as a raw glyph. This file's whole
# subject is encoding damage: if the source were ever re-encoded badly, a literal
# glyph would silently stop matching the thing it is supposed to detect, and every
# assertion below would keep passing while testing nothing.
REPLACEMENT_CHAR = "\ufffd"


def _make_row(**kwargs) -> dict:
    """A minimally valid raw SECOP row, overridable field by field.

    Shared by every class in this module — the mapper needs the same six fields
    populated for any test that expects a non-None result, so there is one factory
    rather than a per-class copy that can drift.
    """
    defaults = {
        "numero_contrato": "CO1.PCCNTR.001",
        "objeto_del_contrato": "Servicios de consultoría en tecnologías de la información",
        "valor_del_contrato": "5000000",
        "fecha_de_inicio_del_contrato": "2024-01-01T00:00:00.000",
        "fecha_de_fin_del_contrato": "2024-12-31T00:00:00.000",
        "nombre_entidad": "MinTIC",
    }
    defaults.update(kwargs)
    return defaults


class TestCalcularValorMensual:
    def test_one_month_contract(self) -> None:
        from app.services.secop_service import _calcular_valor_mensual

        valor = Decimal("3000000")
        inicio = date(2024, 1, 1)
        fin = date(2024, 2, 1)  # ~31 days → 1 month
        result = _calcular_valor_mensual(valor, inicio, fin)
        assert result == Decimal("3000000.00")

    def test_twelve_month_contract(self) -> None:
        from app.services.secop_service import _calcular_valor_mensual

        valor = Decimal("36000000")
        inicio = date(2024, 1, 1)
        fin = date(2025, 1, 1)  # 365 days → ~12 months
        result = _calcular_valor_mensual(valor, inicio, fin)
        assert result == Decimal("3000000.00")

    def test_zero_days_returns_total(self) -> None:
        """Edge case: same start and end date → 0 days → max(1,0)=1 month."""
        from app.services.secop_service import _calcular_valor_mensual

        valor = Decimal("5000000")
        d = date(2024, 6, 1)
        result = _calcular_valor_mensual(valor, d, d)
        assert result == valor.quantize(Decimal("0.01"))


class TestMapearAContratoCreate:
    def test_valid_row_returns_contrato_create(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        result = _mapear_a_contrato_create(_make_row())
        assert result is not None
        assert result.numero_contrato == "CO1.PCCNTR.001"
        assert result.entidad == "MinTIC"

    def test_missing_numero_contrato_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(numero_contrato="", referencia_del_contrato="", id_contrato="")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_short_objeto_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(objeto_del_contrato="Corto")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_zero_valor_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(valor_del_contrato="0")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_negative_valor_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(valor_del_contrato="-100")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_invalid_valor_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(valor_del_contrato="no-es-numero")
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_missing_dates_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(fecha_de_inicio_del_contrato=None, fecha_de_fin_del_contrato=None)
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_end_before_start_returns_none(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(
            fecha_de_inicio_del_contrato="2024-12-31T00:00:00.000",
            fecha_de_fin_del_contrato="2024-01-01T00:00:00.000",
        )
        result = _mapear_a_contrato_create(row)
        assert result is None

    def test_uses_referencia_del_contrato_as_fallback(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(numero_contrato="", referencia_del_contrato="CO1.REF.999")
        result = _mapear_a_contrato_create(row)
        assert result is not None
        assert result.numero_contrato == "CO1.REF.999"

    def test_supervisor_is_mapped(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(nombre_supervisor="Juan Supervisor")
        result = _mapear_a_contrato_create(row)
        assert result is not None
        assert result.supervisor_nombre == "Juan Supervisor"

    def test_calculates_valor_mensual(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(valor_del_contrato="12000000")
        result = _mapear_a_contrato_create(row)
        assert result is not None
        # 364 days ≈ 12 months → ~1000000/month
        assert result.valor_mensual > Decimal("0")

    def test_accented_entidad_passes_through_unmodified(self) -> None:
        """Clean UTF-8 Spanish text (tildes, Ñ) must survive the mapping unchanged.

        Baseline sanity check for the ingestion path: this mapper does not mangle
        well-formed accented text. That is all this test proves — it says nothing
        about where a U+FFFD found in stored data came from.
        """
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(
            nombre_entidad="SANTIAGO DE CALI DISTRITO ESPECIAL - SECRETARÍA DE GESTIÓN DEL "
            "RIESGO DE EMERGENCIAS Y DESASTRES",
            nombre_supervisor="María José Núñez Pérez",
        )
        result = _mapear_a_contrato_create(row)
        assert result is not None
        assert result.entidad == (
            "SANTIAGO DE CALI DISTRITO ESPECIAL - SECRETARÍA DE GESTIÓN DEL RIESGO DE EMERGENCIAS Y DESASTRES"
        )
        assert result.supervisor_nombre == "María José Núñez Pérez"


class TestMapearAContratoCreateReplacementChar:
    """`_mapear_a_contrato_create` surfaces U+FFFD in SECOP text fields.

    Scope of what these tests prove, stated plainly because the previous version of
    this file did not: they prove the DETECTOR's behavior — that a replacement
    character in a raw row produces a structured warning, on the raw (untruncated)
    value, and that the value itself is passed through verbatim rather than guessed
    at. They prove NOTHING about where such a character originates.

    That distinction matters, because the earlier claim in this file — that SECOP's
    Socrata dataset (jbjy-vk9h) serves U+FFFD baked into `nombre_entidad` for NIT
    890399011 — turned out to be wrong. It is contradicted by
    `test_live_secop_returns_accented_entity_name` below, which performs the actual
    query instead of citing one: SECOP returns that entity name correctly accented.
    A U+FFFD reaching our database is therefore something to trace back to a decoder
    on our side of the boundary, not to write off as the government's problem.

    All log assertions patch `secop_service.log` directly rather than using
    `structlog.testing.capture_logs()`. Same reason as TestGap2024Warning in
    test_secop_service_documentos.py: `cache_logger_on_first_use=True` (app/main.py)
    caches this module's logger the first time anything in the suite logs through it,
    which makes `capture_logs()` order-dependent across a full-suite run.
    """

    def test_replacement_char_in_entidad_logs_warning_and_is_preserved(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        corrupted = (
            "SANTIAGO DE CALI DISTRITO ESPECIAL - SECRETAR"
            + REPLACEMENT_CHAR
            + "A DE GESTI"
            + REPLACEMENT_CHAR
            + "N DEL RIESGO"
        )
        row = _make_row(nombre_entidad=corrupted, nit_entidad="890399011")

        with patch("app.services.secop_service.log") as mock_log:
            result = _mapear_a_contrato_create(row)

        assert result is not None
        # Preserved verbatim — no guessed/transformed value, since that would be lossy.
        assert result.entidad == corrupted

        mock_log.warning.assert_any_call(
            "secop_field_replacement_char",
            field="nombre_entidad",
            numero_contrato="CO1.PCCNTR.001",
            nit_entidad="890399011",
            value_preview=corrupted[:120],
        )

    def test_replacement_char_in_objeto_logs_warning(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        objeto_corrupto = (
            "Prestaci"
            + REPLACEMENT_CHAR
            + "n de servicios profesionales de apoyo a la gesti"
            + REPLACEMENT_CHAR
            + "n documental"
        )
        row = _make_row(objeto_del_contrato=objeto_corrupto)

        with patch("app.services.secop_service.log") as mock_log:
            result = _mapear_a_contrato_create(row)

        assert result is not None
        assert result.objeto == objeto_corrupto

        fields_warned = {c.kwargs.get("field") for c in mock_log.warning.call_args_list}
        assert "objeto_del_contrato" in fields_warned

    def test_clean_entidad_does_not_warn(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        with patch("app.services.secop_service.log") as mock_log:
            result = _mapear_a_contrato_create(_make_row(nombre_entidad="MinTIC"))

        assert result is not None
        assert mock_log.warning.call_count == 0

    def test_multiple_corrupted_fields_each_logged(self) -> None:
        from app.services.secop_service import _mapear_a_contrato_create

        row = _make_row(
            nombre_entidad="SECRETAR" + REPLACEMENT_CHAR + "A DE EDUCACI" + REPLACEMENT_CHAR + "N",
            numero_contrato="CO1.PCCNTR." + REPLACEMENT_CHAR + "01",
            nombre_supervisor="Jos" + REPLACEMENT_CHAR + " P" + REPLACEMENT_CHAR + "rez",
        )

        with patch("app.services.secop_service.log") as mock_log:
            result = _mapear_a_contrato_create(row)

        assert result is not None
        fields_warned = {c.kwargs.get("field") for c in mock_log.warning.call_args_list}
        assert fields_warned == {"nombre_entidad", "numero_contrato", "nombre_supervisor"}

    def test_corruption_past_the_objeto_truncation_cutoff_is_still_detected(self) -> None:
        """The mapper caps `objeto` at 2000 chars. Detection must see the RAW value.

        Regression guard: the detector used to run on the already-sliced string, so a
        replacement character living past the cutoff was invisible — the row was
        stored (truncated, so the damage was gone from THIS column, but the source row
        was still corrupt) with no warning at all.
        """
        from app.services.secop_service import _mapear_a_contrato_create

        objeto_largo = ("Prestación de servicios profesionales. " * 60) + REPLACEMENT_CHAR + " cola"
        assert len(objeto_largo) > 2000, "fixture must exceed the 2000-char cap to be meaningful"
        assert REPLACEMENT_CHAR not in objeto_largo[:2000], "the marker must sit past the cutoff"

        row = _make_row(objeto_del_contrato=objeto_largo)

        with patch("app.services.secop_service.log") as mock_log:
            result = _mapear_a_contrato_create(row)

        assert result is not None
        # The stored value is truncated and therefore clean...
        assert REPLACEMENT_CHAR not in result.objeto
        # ...but the warning still fired, because detection saw the raw row value.
        fields_warned = {c.kwargs.get("field") for c in mock_log.warning.call_args_list}
        assert "objeto_del_contrato" in fields_warned

    def test_corruption_past_the_numero_truncation_cutoff_is_still_detected(self) -> None:
        """Same guard for `numero_contrato`, which is capped at 100 chars."""
        from app.services.secop_service import _mapear_a_contrato_create

        numero_largo = ("CO1.PCCNTR." + ("9" * 120)) + REPLACEMENT_CHAR
        assert REPLACEMENT_CHAR not in numero_largo[:100]

        row = _make_row(numero_contrato=numero_largo)

        with patch("app.services.secop_service.log") as mock_log:
            result = _mapear_a_contrato_create(row)

        assert result is not None
        assert REPLACEMENT_CHAR not in result.numero_contrato
        fields_warned = {c.kwargs.get("field") for c in mock_log.warning.call_args_list}
        assert "numero_contrato" in fields_warned


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_secop_returns_accented_entity_name() -> None:
    """Live probe (opt-in: `-m live`) — the evidence for "is SECOP itself corrupt?".

    This test exists because an earlier change asserted, in prose spread across two
    docstrings that cited each other, that SECOP's own dataset serves U+FFFD for NIT
    890399011's "Secretaría de Gestión del Riesgo". Neither docstring contained a
    query. This one runs the query.

    It is an EXHAUSTIVE check, not a sample: the aggregate below groups every row for
    that NIT by entity name, so it covers the NIT's full population rather than
    whatever the first page happens to contain.

    Result recorded 2026-09-09 (direct `httpx` call, no app token, no wrapper):

    - 28 distinct `nombre_entidad` values, covering all 217,544 rows for the NIT.
    - ZERO of them contain U+FFFD, and the byte sequence `ef bf bd` appears nowhere
      in the response body.
    - The specific entity that was blamed comes back correctly accented:
      `SANTIAGO DE CALI DISTRITO ESPECIAL - SECRETARÍA DE GESTIÓN DEL RIESGO DE
      EMERGENCIAS Y DESASTRE`, i.e. U+00CD and U+00D3, on the wire as the UTF-8 byte
      pairs `c3 8d` and `c3 93`. The response declares
      `content-type: application/json;charset=utf-8`.

    So the "SECOP already corrupted it, nothing we can do" conclusion does not hold
    for this record. A U+FFFD seen in our data for it was introduced somewhere on our
    side — a lossy `decode(..., errors="replace")`, or a console re-encode while
    someone was looking at the value.

    If SECOP ever does start serving U+FFFD here, this test fails and says so.
    Socrata throttles aggressively; an unreachable API skips rather than fails, so a
    red result always means the DATA changed, never that the network was busy.
    """
    import httpx

    url = "https://www.datos.gov.co/resource/jbjy-vk9h.json"
    params = {
        "$select": "nombre_entidad, count(1) as n",
        "$where": "nit_entidad = '890399011'",
        "$group": "nombre_entidad",
        "$limit": "500",
    }

    response = None
    async with httpx.AsyncClient(timeout=280.0) as client:
        for _attempt in range(3):
            try:
                response = await client.get(url, params=params)
            except httpx.HTTPError:
                continue
            if response.status_code == 200:
                break
            response = None

    if response is None:
        pytest.skip("datos.gov.co unreachable or throttled — infrastructure, not a data change")

    # Assert on the RAW bytes, not only the parsed strings. This is the exact claim
    # that was gotten wrong before, and raw bytes cannot be confused with whatever a
    # terminal decided to render.
    assert b"\xef\xbf\xbd" not in response.content, (
        "SECOP served U+FFFD bytes for NIT 890399011 — the upstream-corruption claim "
        "would then be correct after all, and this repo's guidance needs revisiting"
    )

    rows = response.json()
    nombres = [row.get("nombre_entidad") or "" for row in rows]
    assert nombres, "expected grouped rows for NIT 890399011"
    assert not [n for n in nombres if REPLACEMENT_CHAR in n], (
        "at least one entity name for this NIT now contains U+FFFD upstream"
    )

    riesgo = [n for n in nombres if "DEL RIESGO" in n]
    assert riesgo, "expected the 'Gestión del Riesgo' entity among this NIT's names"
    assert "GESTIÓN" in riesgo[0], f"expected the accented form, got {riesgo[0]!r}"
    assert "SECRETARÍA" in riesgo[0], f"expected the accented form, got {riesgo[0]!r}"
