"""Tests para evidence_filter_node — heurísticas y gate LLM trabajo/ruido."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Helpers de construcción de items de evidence_raw ─────────────────────────

def _email_item(
    title: str,
    sender: str = "",
    labels: list | None = None,
    content: str = "",
    headers: dict | None = None,
) -> dict:
    meta = {
        "sender": sender,
        "labels": labels or [],
        "title": title,
        "content": content,
        "headers": headers or {},
    }
    return {"source": "email", "title": title, "content": content, "link": "", "date": "", "metadata": meta}


def _calendar_item(title: str, attendees: list | None = None, is_all_day: bool = False) -> dict:
    cal_meta = {"attendees": attendees or [], "is_all_day": is_all_day, "event_type": "default", "organizer": {}}
    item_meta = {"title": title, "metadata": cal_meta}
    return {"source": "calendar", "title": title, "content": title, "link": "", "date": "", "metadata": item_meta}


def _drive_item(title: str, mime: str = "application/pdf") -> dict:
    meta = {"mime_type": mime, "title": title}
    return {"source": "drive", "title": title, "content": title, "link": "", "date": "", "metadata": meta}


# ── Heurísticas deterministas ────────────────────────────────────────────────

def test_heuristic_drops_noreply_sender():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _email_item("Informe mensual", sender="no-reply@promo.com")
    assert _heuristic_is_noise(item) is True


def test_heuristic_drops_promo_sender():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _email_item("Entrega de documentos", sender="promo@deals.io")
    assert _heuristic_is_noise(item) is True


def test_heuristic_drops_promotion_gmail_label():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _email_item("Oferta especial", sender="info@tienda.com", labels=["CATEGORY_PROMOTIONS"])
    assert _heuristic_is_noise(item) is True


def test_heuristic_drops_noise_subject():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _email_item("Factura #12345", sender="cobros@plataforma.com")
    assert _heuristic_is_noise(item) is True


def test_heuristic_keeps_supervisor_email():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _email_item("Informe mensual de actividades", sender="supervisor@entidad.gov.co")
    assert _heuristic_is_noise(item) is False


def test_heuristic_drops_declined_calendar_event():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    attendees = [{"self": True, "responseStatus": "declined"}, {"self": False, "responseStatus": "accepted"}]
    item = _calendar_item("Reunión de seguimiento", attendees=attendees)
    assert _heuristic_is_noise(item) is True


def test_heuristic_drops_allday_event_no_external_attendees():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _calendar_item("Bloqueo de agenda", is_all_day=True, attendees=[{"self": True}])
    assert _heuristic_is_noise(item) is True


def test_heuristic_keeps_allday_event_with_external_attendees():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    attendees = [{"self": True, "responseStatus": "accepted"}, {"self": False, "email": "colega@entidad.gov.co"}]
    item = _calendar_item("Jornada de trabajo conjunta", is_all_day=True, attendees=attendees)
    assert _heuristic_is_noise(item) is False


def test_heuristic_drops_festivo_calendar():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _calendar_item("Día festivo")
    assert _heuristic_is_noise(item) is True


def test_heuristic_keeps_work_meeting():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    attendees = [{"self": True, "responseStatus": "accepted"}, {"self": False}]
    item = _calendar_item("Reunión de seguimiento contractual", attendees=attendees)
    assert _heuristic_is_noise(item) is False


def test_heuristic_drops_drive_folder():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _drive_item("Carpeta de evidencias", mime="application/vnd.google-apps.folder")
    assert _heuristic_is_noise(item) is True


def test_heuristic_keeps_drive_pdf():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise
    item = _drive_item("Informe de actividades.pdf", mime="application/pdf")
    assert _heuristic_is_noise(item) is False


# ── Gate LLM ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_llm_gate_keeps_trabajo():
    from app.agent.nodes.evidence_filter import _llm_classify_batch

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=MagicMock(content='[{"idx": 0, "verdict": "TRABAJO"}]'))

    items = [_email_item("Acta de reunión", sender="coord@entidad.gov.co")]
    result = await _llm_classify_batch(items, llm)
    assert result == [True]


@pytest.mark.asyncio
async def test_llm_gate_drops_ruido():
    from app.agent.nodes.evidence_filter import _llm_classify_batch

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=MagicMock(content='[{"idx": 0, "verdict": "RUIDO"}]'))

    items = [_email_item("Gran oferta en cursos online", sender="mkt@plataforma.com")]
    result = await _llm_classify_batch(items, llm)
    assert result == [False]


@pytest.mark.asyncio
async def test_llm_gate_keeps_on_llm_error():
    """Si el LLM lanza excepción, se conservan todos los items (safe default)."""
    from app.agent.nodes.evidence_filter import _llm_classify_batch

    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=RuntimeError("LLM timeout"))

    items = [_email_item("Algo ambiguo"), _calendar_item("Reunión X")]
    result = await _llm_classify_batch(items, llm)
    assert result == [True, True]


@pytest.mark.asyncio
async def test_llm_gate_keeps_on_invalid_json():
    """Si el LLM devuelve JSON inválido, se conservan todos los items."""
    from app.agent.nodes.evidence_filter import _llm_classify_batch

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=MagicMock(content="Lo siento, no puedo clasificar eso."))

    items = [_email_item("Informe mensual")]
    result = await _llm_classify_batch(items, llm)
    assert result == [True]


# ── Nodo completo ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evidence_filter_node_drops_heuristic_noise_without_llm():
    """Un email de noreply es descartado por heurística sin hacer llamada LLM."""
    from app.agent.nodes import evidence_filter as mod

    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=MagicMock(content="[]"))

    state = {
        "evidence_raw": [
            _email_item("Informe mensual", sender="supervisor@entidad.gov.co"),
            _email_item("Gran oferta", sender="no-reply@promo.com"),
        ]
    }

    with patch.object(mod, "get_llm", return_value=llm):
        result = await mod.evidence_filter_node(state)

    kept = result["evidence_raw"]
    assert len(kept) == 1
    assert kept[0]["title"] == "Informe mensual"
    assert result["evidencias_descartadas"] == 1


@pytest.mark.asyncio
async def test_evidence_filter_node_empty_state_returns_zero():
    from app.agent.nodes import evidence_filter as mod

    llm = AsyncMock()
    state = {"evidence_raw": []}

    with patch.object(mod, "get_llm", return_value=llm):
        result = await mod.evidence_filter_node(state)

    assert result["evidence_raw"] == []
    assert result["evidencias_descartadas"] == 0


# ── score_non_personal_email — sistema de scoring por headers ─────────────────

def test_score_list_unsubscribe_header_returns_5():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="news@newsletter.com",
        subject="Novedades de la semana",
        labels=[],
        headers={"List-Unsubscribe": "<mailto:unsub@newsletter.com>"},
    )
    assert score == 5
    assert "list-unsubscribe" in reason


def test_score_precedence_bulk_header_returns_5():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="info@service.com",
        subject="Actualización mensual",
        labels=[],
        headers={"Precedence": "bulk"},
    )
    assert score == 5
    assert "Precedence" in reason


def test_score_xmailer_sendgrid_returns_5():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="campaigns@brand.com",
        subject="Tu campaña",
        labels=[],
        headers={"X-Mailer": "SendGrid v3.0"},
    )
    assert score == 5
    assert "sendgrid" in reason.lower()


def test_score_github_platform_header_returns_5():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="notifications@github.com",
        subject="[repo] Pull request merged",
        labels=[],
        headers={"X-GitHub-Reason": "subscribed"},
    )
    assert score == 5
    assert "x-github-reason" in reason


def test_score_gmail_promotions_label_returns_5():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="offers@tienda.com",
        subject="Oferta exclusiva",
        labels=["CATEGORY_PROMOTIONS"],
    )
    assert score == 5
    assert "CATEGORY_PROMOTIONS" in reason


def test_score_linkedin_domain_returns_4():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="messages-noreply@linkedin.com",
        subject="Alguien vio tu perfil",
        labels=[],
    )
    assert score >= 4
    assert "linkedin" in reason


def test_score_noreply_prefix_returns_3():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="noreply@internal-tool.io",
        subject="Estado del sistema",
        labels=[],
    )
    assert score == 3
    assert "auto_prefix" in reason


def test_score_no_reply_hyphen_normalizes():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="no-reply@service.com",
        subject="Notificación",
        labels=[],
    )
    assert score >= 3


def test_score_personal_supervisor_email_returns_zero():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="maria.garcia@entidad.gov.co",
        subject="Revisión del informe de actividades",
        labels=[],
    )
    assert score == 0
    assert reason == ""


def test_score_personal_with_noise_subject_alone_is_low():
    """A single weak subject signal must not reach threshold without sender signals."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="juan@empresa.com",
        subject="Bienvenido",
        labels=[],
    )
    assert score < 3


def test_score_high_confidence_subject_plus_service_sender_filters():
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="cobros@plataforma.com",
        subject="Factura #12345",
        labels=[],
    )
    assert score >= 3


def test_score_header_case_insensitive():
    """Header names must be matched case-insensitively."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="news@example.com",
        subject="Newsletter",
        labels=[],
        headers={"LIST-UNSUBSCRIBE": "<mailto:unsub@example.com>"},
    )
    assert score == 5


def test_heuristic_uses_headers_when_present():
    """_heuristic_is_noise must propagate headers from metadata to the scorer."""
    from app.agent.nodes.evidence_filter import _heuristic_is_noise

    item = _email_item(
        title="Novedades del mes",
        sender="info@company.com",
        headers={"List-Unsubscribe": "<mailto:unsub@company.com>"},
    )
    assert _heuristic_is_noise(item) is True


def test_heuristic_personal_with_headers_not_filtered():
    from app.agent.nodes.evidence_filter import _heuristic_is_noise

    item = _email_item(
        title="Revisión del contrato",
        sender="supervisor@entidad.gov.co",
        headers={"Message-ID": "<abc123@entidad.gov.co>"},
    )
    assert _heuristic_is_noise(item) is False


# ── Whitelist — personal providers and institutional domains ──────────────────

def test_score_gmail_sender_never_filtered():
    """Emails from gmail.com must always pass through regardless of subject signals."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="jocelyn.danna@gmail.com",
        subject="Invitación: Revisión Marketing mié 24 jun",
        labels=[],
        headers={"List-Unsubscribe": "<mailto:unsub@gmail.com>"},
    )
    assert score == 0


def test_score_gov_co_domain_never_filtered():
    """Emails from .gov.co domains must always pass through."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="supervisor@entidad.cali.gov.co",
        subject="Aprobación de informe mensual",
        labels=[],
    )
    assert score == 0


def test_score_outlook_sender_never_filtered():
    """Emails from outlook.com must always pass through."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="colega@outlook.com",
        subject="Reunión de seguimiento",
        labels=[],
    )
    assert score == 0


# ── Bank / financial-institution notification senders ─────────────────────────

def test_score_davibank_sender_filtered():
    """Bank notification senders like DAVIbankInforma@davibank.com must be filtered."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, reason = score_non_personal_email(
        sender="DAVIbankInforma <DAVIbankInforma@davibank.com>",
        subject="Información de tu cuenta",
        labels=[],
    )
    assert score >= 3
    assert reason != ""


def test_score_informa_notification_prefix_filtered():
    """The '<Entidad>Informa@' notification-sender pattern is treated as noise."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="EmpresaInforma@servicios.co",
        subject="Aviso importante",
        labels=[],
    )
    assert score >= 3


def test_score_generic_bank_domain_filtered():
    """Any non-whitelisted bank domain is filtered."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="alertas@bankprovider.com",
        subject="Movimiento en tu cuenta",
        labels=[],
    )
    assert score >= 3


def test_score_personal_gmail_with_bank_word_still_kept():
    """A bank keyword must NOT override the personal-provider whitelist."""
    from app.agent.prompts.evidence_filter import score_non_personal_email

    score, _ = score_non_personal_email(
        sender="banco.fer@gmail.com",
        subject="Hola, te paso el informe",
        labels=[],
    )
    assert score == 0
