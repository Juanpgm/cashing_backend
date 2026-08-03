"""Word-boundary keyword matching (R1, clasificacion-documentos-refuerzo).

Guards `keyword_score`/`keyword_hits` against substring false positives
(e.g. "rp" inside "cuerpo") while still matching hyphenated/underscore-joined
tokens and multi-word phrases. See openspec/changes/clasificacion-documentos-refuerzo/.
"""

from decimal import Decimal

from app.core.text_match import keyword_hits, keyword_score


def test_short_keyword_does_not_match_inside_longer_word() -> None:
    assert keyword_hits(["cuerpo del documento"], ["rp"]) == []
    assert keyword_hits(["seccion tercera"], ["cc"]) == []
    assert keyword_hits(["anexo del proyecto"], ["cto"]) == []
    assert keyword_hits(["producto final"], ["cto"]) == []
    assert keyword_hits(["salario bruto"], ["rut"]) == []
    assert keyword_hits(["recopilacion de evidencias"], ["pila"]) == []


def test_keyword_score_is_zero_for_substring_only_false_positives() -> None:
    assert keyword_score(["cuerpo del documento"], ["rp"]) == Decimal("0.000")
    assert keyword_score(["salario bruto", "producto final"], ["rut", "cto"]) == Decimal("0.000")


def test_hyphenated_keyword_still_matches_hyphenated_token() -> None:
    assert keyword_hits(["ds-045.pdf"], ["ds-"]) == ["ds-"]
    assert keyword_hits(["cdp-045.pdf"], ["cdp-"]) == ["cdp-"]


def test_underscore_and_dot_count_as_word_boundaries() -> None:
    assert keyword_hits(["cto_consultor.pdf"], ["cto"]) == ["cto"]
    assert keyword_hits(["rut_contribuyente.pdf"], ["rut"]) == ["rut"]
    assert keyword_hits(["RPC_4500.pdf"], ["rpc"]) == ["rpc"]


def test_multiword_keyword_matches_only_adjacent_token_phrase() -> None:
    assert keyword_hits(["REGISTRO PRESUPUESTAL No. 123"], ["registro presupuestal"]) == ["registro presupuestal"]
    # Same words, not adjacent -> no phrase match.
    assert keyword_hits(["registro de otro presupuestal"], ["registro presupuestal"]) == []


def test_whole_word_keywords_still_match_as_before() -> None:
    assert keyword_hits(["CONTRATO DE PRESTACION DE SERVICIOS"], ["contrato"]) == ["contrato"]
    assert keyword_score(["CONTRATO DE PRESTACION DE SERVICIOS"], ["contrato"]) == Decimal("1.000")


def test_keyword_score_contract_unchanged_fraction_of_keywords_hit() -> None:
    # 1 of 2 keywords hits -> 0.500 (same contract as before: len(hits)/len(kws)).
    assert keyword_score(["cuerpo del contrato"], ["rp", "contrato"]) == Decimal("0.500")
    assert keyword_score([], ["contrato"]) == Decimal("0.000")
    assert keyword_score(["contrato"], []) == Decimal("0.000")


def test_keyword_glued_to_digits_still_matches() -> None:
    # RELIABILITY-001: a digit directly adjacent to the keyword must count as a
    # boundary (unlike a letter), or filename auto-detection breaks for short
    # keywords glued to an id/date with no separator (e.g. "rut1140836527.pdf").
    assert keyword_hits(["rut1140836527.pdf"], ["rut"]) == ["rut"]
    assert keyword_hits(["1140836527rut.pdf"], ["rut"]) == ["rut"]
    assert keyword_hits(["cdp2025.pdf"], ["cdp"]) == ["cdp"]
    assert keyword_score(["rut1140836527.pdf"], ["rut"]) == Decimal("1.000")
