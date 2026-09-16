"""Tests for contract_terms — contract-number variants + combined search terms.

Root cause (evidencias/discovery-fix): a dotted DAGMA/Cali-style contract number
like "4161.010.26.1.027.2025" never survives `_extract_keywords` (dots become
spaces, tokens <=4 chars are dropped) and `_keyword_score`'s `{4,}`-letters-only
regex never matches digits — so the contract number itself was NEVER usable as a
search or scoring signal. These variants close that gap.
"""

from __future__ import annotations

from app.agent.prompts.contract_terms import contract_header, contract_number_variants, contract_search_terms


class TestContractNumberVariants:
    def test_none_returns_empty(self) -> None:
        assert contract_number_variants(None) == []

    def test_empty_string_returns_empty(self) -> None:
        assert contract_number_variants("") == []

    def test_whitespace_only_returns_empty(self) -> None:
        assert contract_number_variants("   ") == []

    def test_bare_four_digit_year_returns_empty(self) -> None:
        """A lone 4-digit year is never a useful search term on its own."""
        assert contract_number_variants("2025") == []

    def test_too_short_number_returns_empty(self) -> None:
        assert contract_number_variants("12") == []

    def test_dotted_dagma_style_number_produces_expected_variants(self) -> None:
        variants = contract_number_variants("4161.010.26.1.027.2025")

        assert variants[0] == "4161.010.26.1.027.2025"  # exact, most specific first
        assert "4161-010-26-1-027-2025" in variants  # dots -> hyphen
        assert "4161 010 26 1 027 2025" in variants  # dots -> space
        assert "41610102610272025" in variants  # dots stripped
        assert "027-2025" in variants  # trailing pair, hyphen
        assert "027.2025" in variants  # trailing pair, dot
        assert "4161" in variants  # first segment (>=4 chars, not a year)

    def test_first_segment_excluded_when_it_is_a_year(self) -> None:
        variants = contract_number_variants("2025.001.002")
        assert "2025" not in variants

    def test_first_segment_excluded_when_too_short(self) -> None:
        variants = contract_number_variants("12.345.2025")
        assert "12" not in variants

    def test_hyphen_slash_style_number(self) -> None:
        variants = contract_number_variants("CTO-123/2025")
        assert "CTO-123/2025" in variants
        assert "CTO-123-2025" in variants
        assert "123-2025" in variants
        assert "123.2025" in variants
        # First-segment variant requires >= 4 chars (spec) — "CTO" (3 chars) is
        # intentionally NOT emitted on its own, it's too short to be a useful term.

    def test_never_emits_short_tokens(self) -> None:
        for v in contract_number_variants("A.1.2025"):
            assert len(v) >= 3

    def test_no_duplicate_variants(self) -> None:
        variants = contract_number_variants("4161.010.26.1.027.2025")
        assert len(variants) == len(set(variants))

    def test_simple_number_without_separators(self) -> None:
        # A single-segment number has no trailing-pair/first-segment variants —
        # only the exact form.
        assert contract_number_variants("CTR12345") == ["CTR12345"]


class TestContractSearchTerms:
    def test_empty_contexto_returns_empty(self) -> None:
        assert contract_search_terms({}) == []

    def test_combines_number_entidad_and_objeto_keywords(self) -> None:
        contexto = {
            "numero_contrato": "4161.010.26.1.027.2025",
            "entidad": "DAGMA S.A.S",
            "objeto": "Prestación de servicios profesionales de apoyo a la gestión ambiental",
        }
        terms = contract_search_terms(contexto)

        assert "4161.010.26.1.027.2025" in terms
        assert "4161" in terms
        assert "DAGMA" in terms  # legal suffix stripped
        assert "S.A.S" not in terms
        assert any("gestión" in t or "ambiental" in t for t in terms)

    def test_missing_entidad_and_objeto_still_returns_number_variants(self) -> None:
        terms = contract_search_terms({"numero_contrato": "4161.010.26.1.027.2025"})
        assert "4161.010.26.1.027.2025" in terms
        assert len(terms) > 0

    def test_missing_numero_still_returns_entidad_and_objeto_terms(self) -> None:
        terms = contract_search_terms({"entidad": "Alcaldía de Cali", "objeto": "Servicios de consultoría jurídica"})
        assert any("Alcaldía" in t or "Cali" in t for t in terms)
        assert any("consultoría" in t or "jurídica" in t for t in terms)

    def test_no_duplicate_terms(self) -> None:
        contexto = {"numero_contrato": "CTO-123/2025", "entidad": "CTO"}
        terms = contract_search_terms(contexto)
        assert len(terms) == len(set(terms))


class TestContractHeader:
    def test_none_returns_empty_string(self) -> None:
        assert contract_header(None) == ""

    def test_empty_dict_returns_empty_string(self) -> None:
        assert contract_header({}) == ""

    def test_includes_numero_entidad_objeto_periodo(self) -> None:
        header = contract_header(
            {
                "numero_contrato": "4161.010.26.1.027.2025",
                "entidad": "DAGMA",
                "objeto": "Prestación de servicios profesionales de apoyo a la gestión",
                "fecha_inicio": "2024-02-01",
                "fecha_fin": "2024-12-31",
            }
        )
        assert "4161.010.26.1.027.2025" in header
        assert "DAGMA" in header
        assert "apoyo a la gestión" in header
        assert "2024-02-01" in header and "2024-12-31" in header

    def test_truncates_long_objeto_to_about_300_chars(self) -> None:
        header = contract_header({"objeto": "x" * 500})
        objeto_line = next(line for line in header.splitlines() if line.startswith("Objeto:"))
        assert len(objeto_line) < 320

    def test_missing_fields_omitted_gracefully(self) -> None:
        header = contract_header({"numero_contrato": "CTR-001"})
        assert "CTR-001" in header
        assert "Entidad" not in header
        assert "Objeto" not in header

    def test_includes_contexto_usuario_line_when_present(self) -> None:
        header = contract_header(
            {"numero_contrato": "CTR-001", "contexto_usuario": "Entregué el informe y asistí a 2 reuniones"}
        )
        assert "Contexto del período (según el contratista)" in header
        assert "Entregué el informe y asistí a 2 reuniones" in header

    def test_omits_contexto_usuario_line_when_absent(self) -> None:
        header = contract_header({"numero_contrato": "CTR-001"})
        assert "Contexto del período" not in header

    def test_truncates_long_contexto_usuario(self) -> None:
        header = contract_header({"contexto_usuario": "y" * 500})
        line = next(line for line in header.splitlines() if line.startswith("Contexto del período"))
        assert len(line) < 350
