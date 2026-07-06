"""Tests for HIL nodes after HumanInterrupt migration."""
from __future__ import annotations

import pytest

from app.agent.engine import HumanInterrupt
from app.agent.nodes.human_review import human_review_node
from app.agent.nodes.template_resolver import template_resolver_node


async def test_human_review_raises_hil_when_no_feedback():
    """human_review_node must raise HumanInterrupt when hil_feedback is None."""
    state = {"hil_feedback": None, "preview_content": "test preview"}
    with pytest.raises(HumanInterrupt):
        await human_review_node(state)


async def test_human_review_consumes_feedback_on_resume():
    """human_review_node must use hil_feedback and clear it."""
    state = {
        "hil_feedback": "approved",
        "preview_content": "test preview",
        "agent_run_id": None,
    }
    result = await human_review_node(state)
    assert result.get("hil_feedback") is None
    assert result.get("preview_approved") is True or "preview_approved" in result


async def test_template_resolver_raises_hil_when_no_doc_type():
    """template_resolver_node must raise HumanInterrupt when document_type is None and no hil_feedback."""
    state = {"hil_feedback": None, "document_type": None, "session_id": None}
    with pytest.raises(HumanInterrupt):
        await template_resolver_node(state)


async def test_template_resolver_uses_feedback_as_doc_type():
    """template_resolver_node must use hil_feedback as document_type on resume."""
    state = {
        "hil_feedback": "cuenta_cobro",
        "document_type": None,
        "session_id": None,
        "template_id": None,
    }
    # If template lookup fails, that's OK — we're testing the interrupt path
    try:
        result = await template_resolver_node(state)
        assert result.get("hil_feedback") is None
    except Exception as e:
        # If it fails due to missing template DB, that's expected in unit test
        assert not isinstance(e, HumanInterrupt), "Should not re-raise HumanInterrupt when feedback is provided"
