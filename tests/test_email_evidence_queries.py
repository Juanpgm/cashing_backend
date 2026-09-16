"""Tests for the Gmail query builders in app.agent.prompts.email_evidence.

Root causes covered (evidencias/discovery-fix):
- every query used to be subject:-scoped, so a contract number or entidad
  mention in the BODY of an email was never found.
- the contract number was never used as a search term at all.
- `-category:updates` excluded legitimate entity notifications (SECOP, Drive
  shares, calendar invites) alongside real promotional noise.
"""

from __future__ import annotations

from app.agent.prompts.email_evidence import (
    GMAIL_NOISE_EXCLUSIONS,
    build_contract_queries,
    build_obligation_queries,
)


class TestGmailNoiseExclusions:
    def test_no_longer_excludes_category_updates(self) -> None:
        """CATEGORY_UPDATES also catches SECOP/Drive-share/calendar-invite mail —
        excluding it server-side killed legitimate entity notifications."""
        assert "-category:updates" not in GMAIL_NOISE_EXCLUSIONS

    def test_still_excludes_promotions_social_forums_spam_trash(self) -> None:
        for term in ("-category:promotions", "-category:social", "-category:forums", "-in:spam", "-in:trash"):
            assert term in GMAIL_NOISE_EXCLUSIONS


class TestBuildContractQueries:
    def test_empty_variants_and_no_entidad_or_supervisor_returns_empty(self) -> None:
        assert build_contract_queries([], "2024/04/01", "2024/04/30") == []

    def test_one_full_text_query_per_variant_no_subject_scoping(self) -> None:
        variants = ["4161.010.26.1.027.2025", "4161"]
        queries = build_contract_queries(variants, "2024/04/01", "2024/04/30")

        for variant in variants:
            assert any(variant in q and "subject:" not in q for q in queries)

    def test_has_attachment_variant_for_exact_number(self) -> None:
        queries = build_contract_queries(["4161.010.26.1.027.2025"], "2024/04/01", "2024/04/30")
        assert any("has:attachment" in q and "4161.010.26.1.027.2025" in q for q in queries)

    def test_entidad_full_text_query_when_present(self) -> None:
        queries = build_contract_queries([], "2024/04/01", "2024/04/30", entidad="DAGMA")
        assert any("DAGMA" in q for q in queries)

    def test_supervisor_from_query_when_present(self) -> None:
        queries = build_contract_queries([], "2024/04/01", "2024/04/30", supervisor_email="sup@entidad.gov.co")
        assert any("from:sup@entidad.gov.co" in q for q in queries)

    def test_date_window_applied_to_every_query(self) -> None:
        queries = build_contract_queries(["4161"], "2024/04/01", "2024/04/30", entidad="DAGMA")
        assert queries
        assert all("after:2024/04/01" in q and "before:2024/04/30" in q for q in queries)

    def test_number_queries_ordered_before_entidad_and_supervisor(self) -> None:
        queries = build_contract_queries(
            ["4161"], "2024/04/01", "2024/04/30", supervisor_email="sup@entidad.gov.co", entidad="DAGMA"
        )
        number_idx = next(i for i, q in enumerate(queries) if "4161" in q)
        entidad_idx = next(i for i, q in enumerate(queries) if "DAGMA" in q)
        supervisor_idx = next(i for i, q in enumerate(queries) if "from:sup@entidad.gov.co" in q)
        assert number_idx < entidad_idx
        assert number_idx < supervisor_idx


class TestBuildObligationQueriesUnscopedBody:
    def test_keyword_query_has_both_subject_and_unscoped_variant(self) -> None:
        queries = build_obligation_queries("Entregar informe mensual de actividades", "2024/04/01", "2024/04/30")
        subject_queries = [q for q in queries if q.startswith("subject:(")]
        unscoped_queries = [q for q in queries if not q.startswith("subject:(") and "informe" in q]
        assert subject_queries
        assert unscoped_queries, f"expected an unscoped body query, got: {queries}"
