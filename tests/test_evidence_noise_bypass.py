"""The contract-number exemption must not disarm the whole noise filter.

Round-2 regression suite for two confirmed findings:

- CRITICAL: `score_non_personal_email` early-returned `(0, "contains_contract
  _number")` as its FIRST statement, before the Gmail category-label check and
  before the List-Unsubscribe / Precedence: bulk / X-Mailer header checks. The
  caller computed the flag with a plain substring test over a variant list
  containing the bare 4-digit dependency code, and the branch deliberately
  FIRES a bare `"4161"` Gmail query — so an entire result set of promotional
  mail entered the pipeline with the deterministic filter fully disabled.
- WARNING: the exemption was wired only at the service pre-filter, while
  `evidence_filter_node` re-scored the same emails WITHOUT it, silently undoing
  the rescue one layer later.
"""

from __future__ import annotations

import pytest

from app.agent.prompts.evidence_filter import score_non_personal_email

DAGMA_NUMERO = "4161.010.26.1.027.2025"


def test_category_label_still_wins_over_the_contract_number_exemption():
    score, reason = score_non_personal_email(
        "promos@retail-blast.com",
        "Tu pedido fue enviado",
        ["CATEGORY_UPDATES"],
        {},
        contains_contract_number=True,
    )
    assert score >= 3, f"promotional label disarmed by the contract-number exemption: {(score, reason)}"


def test_bulk_headers_still_win_over_the_contract_number_exemption():
    score, reason = score_non_personal_email(
        "news@marketing-blast.com",
        f"Boletin semanal {DAGMA_NUMERO}",
        [],
        {"list-unsubscribe": "<mailto:x>"},
        contains_contract_number=True,
    )
    assert score >= 3, f"List-Unsubscribe disarmed by the contract-number exemption: {(score, reason)}"


def test_contract_number_still_rescues_an_otherwise_suspicious_sender():
    """The exemption must keep working where it was meant to: a non-whitelisted
    entity address with an auto prefix, carrying the contract number."""
    score, reason = score_non_personal_email(
        "notificaciones@interventoria-consorcio.com",
        f"Observaciones al informe contrato {DAGMA_NUMERO}",
        [],
        {},
        contains_contract_number=True,
    )
    assert score < 3, f"real entity correspondence dropped: {(score, reason)}"


def test_ms_path_also_keeps_the_classifier_flag_above_the_exemption():
    from app.agent.prompts.evidence_filter import score_non_personal_ms_email

    score, reason = score_non_personal_ms_email(
        sender="promos@retail-blast.com",
        subject="Oferta",
        categories=[],
        inference_classification="other",
        contains_contract_number=True,
    )
    assert score >= 3, f"Graph clutter classifier disarmed by the exemption: {(score, reason)}"


# ── The node must not undo the service-layer rescue ───────────────────────────


def test_filter_node_threads_the_contract_number_through_the_heuristic():
    """`_heuristic_is_noise` re-scored emails without `contains_contract_number`
    and without `supervisor_domain`, so the WU4 bypass was reachable at the
    service pre-filter and then reverted one layer later."""
    from app.agent.nodes.evidence_filter import _heuristic_is_noise

    item = {
        "source": "email",
        "title": f"Observaciones al informe contrato {DAGMA_NUMERO}",
        "content": "Adjunto observaciones",
        "metadata": {
            "sender": "info@contratistas-cali.com",
            "labels": [],
            "headers": {},
        },
    }
    assert _heuristic_is_noise(item, numero_variants=[DAGMA_NUMERO]) is False


def test_filter_node_heuristic_still_drops_noise_when_no_number_is_present():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise

    item = {
        "source": "email",
        "title": "Promo semanal",
        "content": "descuentos",
        "metadata": {"sender": "info@contratistas-cali.com", "labels": [], "headers": {}},
    }
    assert _heuristic_is_noise(item, numero_variants=[DAGMA_NUMERO]) is True


def test_filter_node_heuristic_exempts_the_supervisor_domain():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise

    item = {
        "source": "email",
        "title": "Remision de soportes",
        "content": "adjunto",
        "metadata": {"sender": "notificaciones@interventoria-consorcio.com", "labels": [], "headers": {}},
    }
    assert _heuristic_is_noise(item, supervisor_domain="interventoria-consorcio.com") is False


def test_filter_node_heuristic_signature_is_backward_compatible():
    """Existing callers pass only the item."""
    from app.agent.nodes.evidence_filter import _heuristic_is_noise

    assert _heuristic_is_noise({"source": "drive", "metadata": {"mime_type": "application/pdf"}}) is False


# ── The work/noise LLM prompt must see the sender ─────────────────────────────


def test_work_noise_prompt_includes_the_sender():
    """The header was added so the model could recognise the contract's own
    entity correspondence, but the sender was never rendered — the model
    literally could not tell a supervisor writing from info@ apart from a blast."""
    from app.agent.nodes.evidence_filter import build_work_noise_prompt

    prompt = build_work_noise_prompt(
        [{"idx": 0, "source": "email", "title": "T", "content": "C", "sender": "supervisor@cali.gov.co"}],
        header="HDR",
    )
    assert "supervisor@cali.gov.co" in prompt


def test_work_noise_prompt_omits_the_sender_field_when_unknown():
    from app.agent.nodes.evidence_filter import build_work_noise_prompt

    prompt = build_work_noise_prompt([{"idx": 0, "source": "drive", "title": "T", "content": "C"}], header="")
    assert "Remitente" not in prompt


def test_work_noise_rubric_mentions_the_contract_number_and_entidad_domain():
    from app.agent.prompts.evidence_filter import WORK_NOISE_SYSTEM_PROMPT

    lowered = WORK_NOISE_SYSTEM_PROMPT.lower()
    assert "número de contrato" in lowered or "numero de contrato" in lowered
    assert "entidad" in lowered


@pytest.mark.asyncio
async def test_filter_node_passes_sender_into_the_llm_batch(monkeypatch):
    from app.agent.nodes import evidence_filter as mod

    captured: list[str] = []

    class _LLM:
        async def complete(self, messages, **kwargs):
            captured.append(messages[1].content)

            class _R:
                content = '[{"idx": 0, "verdict": "TRABAJO"}]'

            return _R()

    monkeypatch.setattr(mod, "get_llm", lambda *a, **k: _LLM())

    state = {
        "evidence_raw": [
            {
                "source": "email",
                "title": "Remision de informe",
                "content": "adjunto el informe",
                "metadata": {"sender": "supervisor@cali.gov.co", "labels": [], "headers": {}},
            }
        ],
    }
    await mod.evidence_filter_node(state)

    assert captured, "the LLM classifier was never called"
    assert "supervisor@cali.gov.co" in captured[0]
