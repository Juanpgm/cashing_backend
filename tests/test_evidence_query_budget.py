"""Query-budget policy: contract-level terms must never starve per-obligación search.

Round-2 regression suite for the confirmed CRITICAL finding "contract-number
variants consume the entire query budget in Gmail, Drive AND Calendar". The
reference case is the real DAGMA/Cali contract number `4161.010.26.1.027.2025`,
whose `contract_number_variants` expansion (7 tokens) used to fill every
provider's budget before a single obligación keyword was reached.

Policy pinned here:
- at most `EVIDENCE_MAX_CONTRACT_QUERIES` contract-level slots per provider
  (raw number + ONE normalized form + entidad + supervisor),
- unmatchable variants (space-joined, digits-glued, last-pair, bare first
  segment) are NEVER used as queries (they stay available for scoring),
- the remaining budget is allocated ROUND-ROBIN across obligaciones so each one
  contributes at least one query in every provider.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

DAGMA_NUMERO = "4161.010.26.1.027.2025"
ENTIDAD = "DAGMA"

# Five obligaciones whose leading verb is distinctive and >4 chars, so it
# survives `_extract_keywords` and is always that obligación's first keyword.
OBLIGACIONES = [
    {"id": "ob1", "descripcion": "Auditar sistemas informáticos gubernamentales locales"},
    {"id": "ob2", "descripcion": "Certificar procesos administrativos regionales anuales"},
    {"id": "ob3", "descripcion": "Diagnosticar infraestructuras comunitarias territoriales rurales"},
    {"id": "ob4", "descripcion": "Evaluar convenios interinstitucionales culturales nacionales"},
    {"id": "ob5", "descripcion": "Fiscalizar plataformas tecnológicas municipales digitales"},
]
OB_KEYWORDS = ["auditar", "certificar", "diagnosticar", "evaluar", "fiscalizar"]

EXPANDED = {
    "ob1": ["informe de auditoria de sistemas"],
    "ob2": ["certificacion de procesos"],
    "ob3": ["diagnostico territorial rural"],
    "ob4": ["acta de convenio interinstitucional"],
    "ob5": ["reporte de plataformas municipales"],
}

# Variants that Gmail/Drive/Calendar can never usefully match as a query:
# a space-joined number is tokenized apart, the digits-glued form appears in no
# document, and the last-pair / bare-first-segment forms are noise magnets.
UNMATCHABLE_VARIANTS = ["4161 010 26 1 027 2025", "41610102610272025", "027-2025", "027.2025", "4161"]

# 20 obligaciones with a DISTINCT leading verb each (routine for a Colombian
# CPS contract) — needed for the ceiling regressions below: reusing near-
# identical descriptions collapses to a handful of queries via round_robin's
# cross-group dedup and never actually exercises the budget.
_DISTINCT_VERBS = [
    "auditar",
    "certificar",
    "diagnosticar",
    "evaluar",
    "fiscalizar",
    "inspeccionar",
    "monitorear",
    "planificar",
    "coordinar",
    "ejecutar",
    "revisar",
    "validar",
    "implementar",
    "gestionar",
    "desarrollar",
    "elaborar",
    "sistematizar",
    "verificar",
    "articular",
    "promover",
]
MANY_OBLIGACIONES = [
    {"id": f"ob{i}", "descripcion": f"{verbo.capitalize()} procesos territoriales comunitarios anuales"}
    for i, verbo in enumerate(_DISTINCT_VERBS)
]


# ── Gmail ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gmail_every_obligacion_gets_a_query_with_a_real_contract_number():
    """The branch's pre-existing invariant test omitted `numero_contrato`, so it
    was structurally blind to the starvation bug. With the real DAGMA number,
    every obligación must still contribute at least one Gmail query."""
    from app.services import evidence_discovery_service as eds

    captured: list[str] = []

    async def _fake_search(usuario_id, query, max_results):
        captured.append(query)
        return []

    adapter = MagicMock()
    adapter.search_messages = AsyncMock(side_effect=_fake_search)

    with patch.object(eds, "GmailAdapter", return_value=adapter):
        await eds._gather_email_evidence(
            MagicMock(),
            uuid.uuid4(),
            OBLIGACIONES,
            "2025-09-01",
            "2025-09-30",
            None,
            ENTIDAD,
            numero_contrato=DAGMA_NUMERO,
            expanded_terms=EXPANDED,
        )

    blob = " ".join(captured).lower()
    for term in OB_KEYWORDS:
        assert term in blob, f"obligación keyword '{term}' never reached Gmail — fired: {captured}"


@pytest.mark.asyncio
async def test_gmail_does_not_spend_slots_on_unmatchable_number_variants():
    """The space-joined/digits-glued/last-pair/bare-segment forms are kept for
    SCORING but must never consume a Gmail query slot."""
    from app.services import evidence_discovery_service as eds

    captured: list[str] = []

    async def _fake_search(usuario_id, query, max_results):
        captured.append(query)
        return []

    adapter = MagicMock()
    adapter.search_messages = AsyncMock(side_effect=_fake_search)

    with patch.object(eds, "GmailAdapter", return_value=adapter):
        await eds._gather_email_evidence(
            MagicMock(),
            uuid.uuid4(),
            OBLIGACIONES,
            "2025-09-01",
            "2025-09-30",
            None,
            ENTIDAD,
            numero_contrato=DAGMA_NUMERO,
        )

    for bad in UNMATCHABLE_VARIANTS:
        assert not any(f'"{bad}"' in q for q in captured), f"unmatchable variant {bad!r} consumed a Gmail slot"


@pytest.mark.asyncio
async def test_gmail_expanded_phrases_reach_the_provider():
    """WU7 semantic expansion is the product's core mechanism — its phrases must
    survive the budget, not land past the truncation point."""
    from app.services import evidence_discovery_service as eds

    captured: list[str] = []

    async def _fake_search(usuario_id, query, max_results):
        captured.append(query)
        return []

    adapter = MagicMock()
    adapter.search_messages = AsyncMock(side_effect=_fake_search)

    with patch.object(eds, "GmailAdapter", return_value=adapter):
        await eds._gather_email_evidence(
            MagicMock(),
            uuid.uuid4(),
            OBLIGACIONES,
            "2025-09-01",
            "2025-09-30",
            None,
            ENTIDAD,
            numero_contrato=DAGMA_NUMERO,
            expanded_terms=EXPANDED,
        )

    blob = " ".join(captured).lower()
    hits = sum(1 for phrases in EXPANDED.values() if phrases[0].lower() in blob)
    assert hits >= 3, f"expected most expanded phrases to be queried, got {hits}/5 — fired: {captured}"


@pytest.mark.asyncio
async def test_gmail_total_queries_never_exceed_the_configured_ceiling():
    """WARNING regression: `obligacion_budget = max(remaining, MIN_PER_OB *
    n_groups)` let the per-obligación floor REQUIREMENT alone decide the
    total once obligaciones outnumbered the budget — with 20 obligaciones
    (routine for a Colombian CPS contract) that meant up to 40
    obligación-side queries alone, regardless of EVIDENCE_MAX_GMAIL_QUERIES.
    The per-obligación floor may still legitimately push a BOUNDED amount
    past the nominal ceiling (never starve an obligación entirely — an
    intentional round-2 decision), but the overrun must now be capped at
    `EVIDENCE_MAX_CONTRACT_QUERIES` instead of scaling with obligación count."""
    from app.core.config import settings
    from app.services import evidence_discovery_service as eds

    many_obligaciones = MANY_OBLIGACIONES
    captured: list[str] = []

    async def _fake_search(usuario_id, query, max_results):
        captured.append(query)
        return []

    adapter = MagicMock()
    adapter.search_messages = AsyncMock(side_effect=_fake_search)

    with patch.object(eds, "GmailAdapter", return_value=adapter):
        await eds._gather_email_evidence(
            MagicMock(),
            uuid.uuid4(),
            many_obligaciones,
            "2025-09-01",
            "2025-09-30",
            None,
            ENTIDAD,
            numero_contrato=DAGMA_NUMERO,
        )

    bound = settings.EVIDENCE_MAX_GMAIL_QUERIES + settings.EVIDENCE_MAX_CONTRACT_QUERIES
    assert len(captured) <= bound, (
        f"fired {len(captured)} Gmail queries for 20 obligaciones, bounded overrun ceiling is {bound} "
        f"(nominal {settings.EVIDENCE_MAX_GMAIL_QUERIES} + at most {settings.EVIDENCE_MAX_CONTRACT_QUERIES} contract slots)"
    )
    # Every obligación must still contribute at least one query — starving an
    # obligación entirely would be worse than the bounded overrun above.
    blob = " ".join(captured).lower()
    for verbo in _DISTINCT_VERBS:
        assert verbo in blob, f"obligación keyword '{verbo}' starved out entirely — fired: {captured}"


# ── Drive ─────────────────────────────────────────────────────────────────────


def _drive_terms(calls) -> list[str]:
    return [call.args[1].keywords[0] for call in calls]


@pytest.mark.asyncio
async def test_drive_every_obligacion_gets_a_query_with_a_real_contract_number():
    from app.agent.nodes import drive_fetch as mod

    adapter = MagicMock()
    adapter.search_files = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2025-09-01",
            "fecha_fin": "2025-09-30",
            "numero_contrato": DAGMA_NUMERO,
            "entidad": ENTIDAD,
        },
        "obligaciones_contexto": OBLIGACIONES,
        "expanded_terms": EXPANDED,
    }

    with patch.object(mod, "DriveAdapter", return_value=adapter):
        await mod.drive_fetch_node(state)

    terms = [t.lower() for t in _drive_terms(adapter.search_files.call_args_list)]
    for kw in OB_KEYWORDS:
        assert any(kw in t for t in terms), f"obligación keyword '{kw}' never reached Drive — fired: {terms}"


@pytest.mark.asyncio
async def test_drive_does_not_spend_slots_on_unmatchable_number_variants():
    from app.agent.nodes import drive_fetch as mod

    adapter = MagicMock()
    adapter.search_files = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2025-09-01",
            "fecha_fin": "2025-09-30",
            "numero_contrato": DAGMA_NUMERO,
        },
        "obligaciones_contexto": OBLIGACIONES,
    }

    with patch.object(mod, "DriveAdapter", return_value=adapter):
        await mod.drive_fetch_node(state)

    terms = _drive_terms(adapter.search_files.call_args_list)
    for bad in UNMATCHABLE_VARIANTS:
        assert bad not in terms, f"unmatchable variant {bad!r} consumed a Drive slot"


@pytest.mark.asyncio
async def test_drive_queries_the_contract_number_once_not_once_per_obligacion():
    """Contract-level terms are a per-RUN signal; repeating them per obligación
    is what consumed the budget."""
    from app.agent.nodes import drive_fetch as mod

    adapter = MagicMock()
    adapter.search_files = AsyncMock(return_value=[])

    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2025-09-01",
            "fecha_fin": "2025-09-30",
            "numero_contrato": DAGMA_NUMERO,
        },
        "obligaciones_contexto": OBLIGACIONES,
    }

    with patch.object(mod, "DriveAdapter", return_value=adapter):
        await mod.drive_fetch_node(state)

    terms = _drive_terms(adapter.search_files.call_args_list)
    assert terms.count(DAGMA_NUMERO) == 1, f"contract number queried {terms.count(DAGMA_NUMERO)}x — fired: {terms}"


@pytest.mark.asyncio
async def test_drive_total_queries_never_exceed_the_configured_ceiling():
    """Same shape as the Gmail ceiling regression: with many obligaciones the
    per-obligación floor REQUIREMENT must be capped instead of scaling the
    fired query count with obligación count. A bounded overrun (at most
    EVIDENCE_MAX_CONTRACT_QUERIES, on top of the always-included generic
    terms) is still acceptable — starving an obligación entirely is not."""
    from app.agent.nodes import drive_fetch as mod
    from app.core.config import settings

    adapter = MagicMock()
    adapter.search_files = AsyncMock(return_value=[])

    many_obligaciones = MANY_OBLIGACIONES
    state = {
        "user_id": uuid.uuid4(),
        "_db": MagicMock(),
        "contrato_contexto": {
            "fecha_inicio": "2025-09-01",
            "fecha_fin": "2025-09-30",
            "numero_contrato": DAGMA_NUMERO,
            "entidad": ENTIDAD,
        },
        "obligaciones_contexto": many_obligaciones,
    }

    with patch.object(mod, "DriveAdapter", return_value=adapter):
        await mod.drive_fetch_node(state)

    fired = adapter.search_files.call_count
    bound = settings.EVIDENCE_MAX_QUERIES_TOTAL + settings.EVIDENCE_MAX_CONTRACT_QUERIES
    assert fired <= bound, f"fired {fired} Drive queries for 20 obligaciones, bounded overrun ceiling is {bound}"


# ── Calendar ──────────────────────────────────────────────────────────────────


def test_calendar_terms_give_every_obligacion_a_slot():
    from app.agent.nodes.calendar_fetch import _calendar_terms

    terms = _calendar_terms(
        {"numero_contrato": DAGMA_NUMERO, "entidad": ENTIDAD},
        OBLIGACIONES,
        EXPANDED,
    )
    blob = " ".join(terms).lower()
    for kw in OB_KEYWORDS:
        assert kw in blob, f"obligación keyword '{kw}' never reached Calendar — terms: {terms}"


def test_calendar_terms_exclude_unmatchable_number_variants():
    from app.agent.nodes.calendar_fetch import _calendar_terms

    terms = _calendar_terms({"numero_contrato": DAGMA_NUMERO, "entidad": ENTIDAD}, OBLIGACIONES, EXPANDED)
    for bad in UNMATCHABLE_VARIANTS:
        assert bad not in terms, f"unmatchable variant {bad!r} consumed a Calendar slot"


def test_calendar_terms_keep_the_raw_contract_number():
    """Dropping the weak variants must not drop the strong one."""
    from app.agent.nodes.calendar_fetch import _calendar_terms

    terms = _calendar_terms({"numero_contrato": DAGMA_NUMERO, "entidad": ENTIDAD}, OBLIGACIONES, EXPANDED)
    assert DAGMA_NUMERO in terms


def test_calendar_terms_never_exceed_the_configured_ceiling():
    """Same shape as the Gmail/Drive ceiling regression: many obligaciones must
    cap the per-obligación floor REQUIREMENT rather than scale the produced
    term count past EVIDENCE_MAX_CALENDAR_TERMS. A bounded overrun (at most
    EVIDENCE_MAX_CONTRACT_QUERIES) is still acceptable."""
    from app.agent.nodes.calendar_fetch import _calendar_terms
    from app.core.config import settings

    many_obligaciones = MANY_OBLIGACIONES
    terms = _calendar_terms({"numero_contrato": DAGMA_NUMERO, "entidad": ENTIDAD}, many_obligaciones, {})
    bound = settings.EVIDENCE_MAX_CALENDAR_TERMS + settings.EVIDENCE_MAX_CONTRACT_QUERIES
    assert len(terms) <= bound, (
        f"produced {len(terms)} Calendar terms for 20 obligaciones, bounded overrun ceiling is {bound}"
    )


# ── The shared helpers ────────────────────────────────────────────────────────


def test_contract_query_variants_keeps_only_matchable_forms():
    from app.agent.prompts.contract_terms import contract_query_variants

    variants = contract_query_variants(DAGMA_NUMERO)
    assert variants[0] == DAGMA_NUMERO
    assert len(variants) <= 2
    for bad in UNMATCHABLE_VARIANTS:
        assert bad not in variants


def test_contract_query_variants_is_a_subset_of_scoring_variants():
    from app.agent.prompts.contract_terms import contract_number_variants, contract_query_variants

    scoring = contract_number_variants(DAGMA_NUMERO)
    for v in contract_query_variants(DAGMA_NUMERO):
        assert v in scoring


def test_contract_query_variants_handles_empty_and_single_segment():
    from app.agent.prompts.contract_terms import contract_query_variants

    assert contract_query_variants(None) == []
    assert contract_query_variants("   ") == []
    assert contract_query_variants("CTO123") == ["CTO123"]


def test_round_robin_gives_every_group_a_slot_before_any_group_gets_two():
    from app.agent.prompts.query_budget import round_robin

    groups = [["a1", "a2", "a3"], ["b1", "b2"], ["c1"]]
    assert round_robin(groups, budget=3) == ["a1", "b1", "c1"]
    assert round_robin(groups, budget=5) == ["a1", "b1", "c1", "a2", "b2"]


def test_round_robin_handles_empty_groups_and_zero_budget():
    from app.agent.prompts.query_budget import round_robin

    assert round_robin([], budget=10) == []
    assert round_robin([[], []], budget=10) == []
    assert round_robin([["a"], ["b"]], budget=0) == []
    # An exhausted group is skipped, not padded.
    assert round_robin([["a"], ["b1", "b2", "b3"]], budget=10) == ["a", "b1", "b2", "b3"]


def test_round_robin_dedupes_across_groups_preserving_first_position():
    """A duplicate must not consume a second slot: group B skips the shared "x"
    and advances to its own next term within the SAME pass, so both groups still
    contribute a distinct term before either gets a second one."""
    from app.agent.prompts.query_budget import round_robin

    assert round_robin([["x", "y"], ["x", "z"]], budget=10) == ["x", "z", "y"]
