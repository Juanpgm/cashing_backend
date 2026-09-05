"""The heading discriminator must be STRUCTURAL, never a typography proxy.

Round-4 review (engram obs #496) reproduced a regression of ``255aea9``: the
score's first element was ``es_encabezado = texto[idx:fin].isupper()`` over the
keyword span alone. ``str.isupper()`` is a typography proxy, not a structure
detector, so every heading a real contract can carry that is not ALL-CAPS was
silently reclassified as a "prose mention" and lost to an UPPERCASE generic
tier-2 clause — round-1's bug, back again.

Three triggers, all realistic:

(1) Title Case heading — Word's default for a numbered clause title.
(2) Sentence case behind a numeric sub-heading ("3. Obligaciones específicas…").
(3) OCR flipping ONE accented char to lowercase ("OBLIGACIONES ESPECíFICAS").
    Keyword matching runs on ``texto.upper()`` so the section is still found;
    only the case-based heading flag breaks.

Every heading in every pre-existing fixture is ALL-CAPS, which is exactly why
the suite stayed green on the wrong axis.

(4) and (5) close the loop on the "document typeset ENTIRELY in uppercase"
limitation the previous docstring declared out of scope: with a structural
signal, case carries no information at all, in either direction.

(6) pins the same failure one level up: "an enumeration marker must appear
within N characters of the keyword" is a THIRD typography proxy. It demotes the
very common "CLÁUSULA … OBLIGACIONES ESPECÍFICAS: El contratista se obliga a …
así:" shape, whose list opens after a paragraph. The enumerated-block half of
the rule belongs where it already lives — a candidate section that yields no
items is dropped before scoring — and without a distance bound.
"""

from __future__ import annotations

import pytest
from app.agent.tools.contract_parser import extract_obligaciones_verbatim

_GENERALES = [
    "Cumplir con el objeto del contrato en los términos pactados por las partes",
    "Obrar con lealtad y buena fe en las distintas etapas contractuales",
    "Afiliarse y cotizar al sistema general de seguridad social integral",
    "Presentar los informes de actividades que le sean solicitados",
    "Mantener vigentes las garantías exigidas durante la ejecución",
    "Guardar la reserva sobre la información que conozca por el contrato",
    "Responder por sus actuaciones y omisiones frente a terceros",
    "Atender los requerimientos de los organismos de control",
    "Reportar oportunamente cualquier situación que afecte la ejecución",
    "Devolver los elementos entregados para el desarrollo de sus funciones",
    "Acatar la Constitución, la ley y los reglamentos internos de la entidad",
    "Las demás que le sean asignadas por la ley y por el supervisor del contrato",
]

_ESPECIFICAS = [
    "Diseñar e implementar los módulos del sistema de información SISBEN IV",
    "Realizar las pruebas unitarias y de integración de cada componente",
    "Capacitar a los funcionarios de la entidad en el uso del nuevo sistema",
    "Documentar los procedimientos técnicos y los manuales de usuario",
    "Las demás actividades que le asigne la supervisión relacionadas con el objeto",
]


def _enumerar(items: list[str]) -> str:
    return "\n".join(f"{i}. {texto}." for i, texto in enumerate(items, start=1))


def _documento(encabezado_especificas: str) -> str:
    """The round-1 document, parameterised on how the ESPECÍFICAS heading is typeset.

    The generic tier-2 clause stays ALL-CAPS with 12 items, so any scoring that
    ranks capitalisation above tier picks the general duties.
    """
    return f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0456

CLÁUSULA PRIMERA — OBJETO: desarrollo del sistema de información.

CLÁUSULA SEGUNDA — OBLIGACIONES DEL CONTRATISTA:

{_enumerar(_GENERALES)}

{encabezado_especificas}

{_enumerar(_ESPECIFICAS)}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: dieciocho millones de pesos.
"""


def _assert_picked_especificas(texto: str) -> None:
    descripciones = [o.descripcion for o in extract_obligaciones_verbatim(texto)]

    assert len(descripciones) == len(_ESPECIFICAS), (
        f"expected the tier-1 ESPECÍFICAS section, got {len(descripciones)} items: {descripciones}"
    )
    assert descripciones[0].startswith("Diseñar e implementar"), descripciones
    assert not any("lealtad y buena fe" in d for d in descripciones), (
        f"general duties persisted as specific obligations: {descripciones}"
    )


class TestHeadingDetectionIsCaseInsensitive:
    def test_1_title_case_heading_still_beats_the_uppercase_generic_clause(self) -> None:
        """Word's default title casing for a clause heading."""
        _assert_picked_especificas(_documento("Cláusula Tercera - Obligaciones Específicas del Contratista:"))

    def test_2_sentence_case_numbered_subheading_still_beats_the_generic_clause(self) -> None:
        """A numbered sub-heading in sentence case ("3. Obligaciones específicas…")."""
        _assert_picked_especificas(_documento("3. Obligaciones específicas del contratista:"))

    def test_3_ocr_lowercasing_one_accented_char_does_not_demote_the_heading(self) -> None:
        """A single mis-recognised "í" must not reclassify a heading as prose."""
        _assert_picked_especificas(_documento("CLÁUSULA TERCERA — OBLIGACIONES ESPECíFICAS DEL CONTRATISTA:"))


# ── The mirror image: an ALL-CAPS document carries no case signal either ──────

_TEXTO_TODO_MAYUSCULAS = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0456

CLÁUSULA PRIMERA — OBJETO: DESARROLLO DEL SISTEMA DE INFORMACIÓN.

CLÁUSULA SEGUNDA — OBLIGACIONES DEL CONTRATISTA:

{_enumerar([d.upper() for d in _GENERALES])}

CLÁUSULA TERCERA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

{_enumerar([d.upper() for d in _ESPECIFICAS])}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: DIECIOCHO MILLONES DE PESOS.
"""

# The case the previous docstring conceded as a "known limitation": when EVERY
# occurrence looks like a heading, a case-based flag ties and the decision falls
# through. Here the tier-1 PARÁGRAFO is pure prose yet carries MORE bullets than
# the real ESPECÍFICAS clause, so the tie falls all the way to item count and the
# prose block wins. Structure decides it; capitalisation never could.
_ESPECIFICAS_CORTAS = [
    "Diseñar e implementar los módulos del sistema de información SISBEN IV",
    "Realizar las pruebas unitarias y de integración de cada componente",
]

_TEXTO_MAYUSCULAS_PROSA_MAS_LARGA = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0999

CLÁUSULA PRIMERA — OBJETO: DESARROLLO DEL SISTEMA DE INFORMACIÓN.

CLÁUSULA SEGUNDA — PARÁGRAFO: PARA LOS EFECTOS DE ESTE CONTRATO,
LAS OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA CONSISTEN EN LO SIGUIENTE:

1. ATENDER LO PREVISTO EN EL MANUAL DE CONTRATACIÓN DE LA ENTIDAD.
2. OBSERVAR LOS LINEAMIENTOS DEL PLAN ANUAL DE ADQUISICIONES.
3. CUMPLIR CON LAS DEMÁS ACTIVIDADES INHERENTES AL OBJETO CONTRACTUAL.

CLÁUSULA TERCERA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

{_enumerar([d.upper() for d in _ESPECIFICAS_CORTAS])}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: DIECIOCHO MILLONES DE PESOS.
"""


class TestUppercaseOnlyDocumentIsStillDecidedByStructure:
    def test_4_all_caps_document_still_prefers_the_tier1_clause(self) -> None:
        descripciones = [o.descripcion for o in extract_obligaciones_verbatim(_TEXTO_TODO_MAYUSCULAS)]

        assert len(descripciones) == len(_ESPECIFICAS), (
            f"expected the tier-1 ESPECÍFICAS section, got {len(descripciones)}: {descripciones}"
        )
        assert descripciones[0].startswith("DISEÑAR E IMPLEMENTAR"), descripciones

    def test_5_all_caps_prose_mention_loses_even_with_more_bullets(self) -> None:
        descripciones = [o.descripcion for o in extract_obligaciones_verbatim(_TEXTO_MAYUSCULAS_PROSA_MAS_LARGA)]

        assert len(descripciones) == len(_ESPECIFICAS_CORTAS), (
            f"expected the real 2-item ESPECÍFICAS heading, got {len(descripciones)}: {descripciones}"
        )
        assert descripciones[0].startswith("DISEÑAR E IMPLEMENTAR"), descripciones
        assert not any("MANUAL DE CONTRATACIÓN" in d for d in descripciones), (
            f"an ALL-CAPS prose mention outranked a real heading: {descripciones}"
        )


class TestHeadingDetectionIsNotAboutLayoutEither:
    def test_6_a_lead_in_paragraph_before_the_list_does_not_demote_the_heading(self) -> None:
        """A heading may introduce its list through a paragraph of its own.

        Requiring an enumeration marker within a fixed distance of the keyword
        is a THIRD typography proxy: it reads "how the clause is laid out", not
        "is this a heading". Colombian contracts routinely open the ESPECÍFICAS
        clause with an "El contratista se obliga a … así:" paragraph before the
        first item, and demoting that to a prose mention hands the document
        straight back to the generic tier-2 clause — round-1's bug again.

        The "followed by an enumerated block" half of the rule is enforced where
        it belongs and without an arbitrary distance: a candidate that yields no
        items is dropped by ``extract_obligaciones_verbatim`` before scoring.
        """
        texto = _documento(
            "CLÁUSULA TERCERA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
            "El contratista se obliga a ejecutar el objeto contractual con plena autonomía\n"
            "técnica y administrativa, sin que exista subordinación laboral alguna con la\n"
            "entidad, y responderá por la calidad de los productos entregados, así:"
        )

        _assert_picked_especificas(texto)


# ── Each half of the prefix rule, isolated ───────────────────────────────────
# A prose mention that opens its own line behind a real clause label is the one
# shape the "begins its own line" test alone cannot reject. Two independent
# signals cover it, and each fixture below is decided by exactly one of them.


def _prosa_tier1_contra_generales(mencion: str) -> str:
    """A tier-1 PROSE block (2 throwaway bullets) racing the real tier-2 list.

    The prose comes FIRST so "the first candidate keeps a tie" cannot rescue the
    outcome: if the mention is scored as a heading it wins on tier and the
    contract's general duties never get a chance.
    """
    return f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2025-0777

CLÁUSULA PRIMERA — OBJETO: desarrollo del sistema de información.

{mencion}
1. Atender lo previsto en el manual de contratación de la entidad.
2. Observar los lineamientos del plan anual de adquisiciones.

CLÁUSULA TERCERA — OBLIGACIONES DEL CONTRATISTA:

{_enumerar(_GENERALES)}

CLÁUSULA CUARTA — VALOR DEL CONTRATO: dieciocho millones de pesos.
"""


def _assert_picked_generales(texto: str) -> None:
    descripciones = [o.descripcion for o in extract_obligaciones_verbatim(texto)]

    assert len(descripciones) == len(_GENERALES), (
        f"expected the real tier-2 12-item list, got {len(descripciones)}: {descripciones}"
    )
    assert descripciones[0].startswith("Cumplir con el objeto"), descripciones
    assert not any("manual de contratación" in d for d in descripciones), (
        f"a prose mention was scored as a heading: {descripciones}"
    )


class TestAClauseLabelDoesNotMakeEverythingAfterItAHeading:
    def test_7_running_text_after_the_colon_that_ends_the_label_is_prose(self) -> None:
        """A ``:`` closes a clause label; what follows it is the clause's BODY.

        "CLÁUSULA SEGUNDA — ALCANCE: las obligaciones específicas…" starts with
        a perfectly good label, so the label check alone accepts it. The colon
        is what says the heading already ended.
        """
        _assert_picked_generales(
            _prosa_tier1_contra_generales(
                "CLÁUSULA SEGUNDA — ALCANCE: las obligaciones específicas del contratista serán las siguientes:"
            )
        )

    def test_8_a_paragrafo_inside_the_label_is_never_a_heading(self) -> None:
        """A PARÁGRAFO qualifies the clause before it; it never opens the list.

        Written with a full stop instead of a colon ("CLÁUSULA SEGUNDA.
        PARÁGRAFO. Las obligaciones específicas…") the colon rule does not fire,
        so PARÁGRAFO itself has to disqualify the prefix.
        """
        _assert_picked_generales(
            _prosa_tier1_contra_generales(
                "CLÁUSULA SEGUNDA. PARÁGRAFO. Las obligaciones específicas del contratista comprenden lo siguiente:"
            )
        )

    def test_9_a_sentence_running_into_the_keyword_is_prose_however_it_is_numbered(self) -> None:
        """A numbered PARAGRAPH is not a numbered HEADING.

        "2." is a valid label, so only the shape of what follows it decides:
        a heading continues its own title for a few words, a paragraph runs on
        into a sentence.
        """
        _assert_picked_generales(
            _prosa_tier1_contra_generales(
                "2. El contratista se obliga a cumplir cabalmente con todas y cada "
                "una de las obligaciones específicas del contratista siguientes:"
            )
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "Undecidable from line structure alone: a SHORT numbered sentence "
            '("3. Conforme a lo anterior, las obligaciones específicas…") is '
            "indistinguishable from a numbered heading whose title carries a few "
            'extra words ("CLÁUSULA SEGUNDA. ALCANCE DEL OBJETO CONTRACTUAL Y '
            'OBLIGACIONES ESPECÍFICAS…") — same label, same word count, same line '
            "shape. The signals that WOULD separate them (the article, the comma, "
            "the conjugated verb) are lexical, not structural, and capitalisation "
            "is exactly the proxy this fix removed. Left failing on purpose rather "
            "than tuned away with a word count that fits this fixture and nothing "
            "else; the blast radius is bounded because the prose block must also "
            "out-tier and out-count the real clause to win."
        ),
    )
    def test_10_a_short_numbered_sentence_is_still_read_as_a_heading(self) -> None:
        _assert_picked_generales(
            _prosa_tier1_contra_generales(
                "3. Conforme a lo anterior, las obligaciones específicas del contratista se detallan así:"
            )
        )
