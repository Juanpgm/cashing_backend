"""Evidence orchestrator node — coordinates email + local file evidence gathering (Phase 4)."""

from __future__ import annotations

import structlog

from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.evidence_orchestrator")


async def evidence_orchestrator_node(state: AgentState) -> AgentState:
    """Orchestrate evidence gathering from all Google Workspace + local sources.

    Consolidates email (Gmail), Drive documents, Calendar events and local uploaded
    files into a single ``evidence_raw`` list with a uniform shape so the matcher and
    the justification step can treat every source the same way. Each entry carries a
    ``link`` (when available) so the final Cuenta de Cobro can cite the evidence.

    Reads: email_evidencias, drive_evidencias, calendar_evidencias, local_evidence
    Writes: evidence_raw, current_phase
    """
    # Gmail evidence (raw emails gathered for the obligations).
    email_evidencias: list[dict] = state.get("email_evidencias") or []
    # Drive documents found by drive_fetch.
    drive_evidencias: list[dict] = state.get("drive_evidencias") or []
    # Calendar events found by calendar_fetch.
    calendar_evidencias: list[dict] = state.get("calendar_evidencias") or []
    # Local evidence from the local_files node.
    local_evidence: list[dict] = state.get("local_evidence") or []

    evidence_raw: list[dict] = []

    for ev in email_evidencias:
        evidence_raw.append(
            {
                "source": "email",
                "content": ev.get("content") or ev.get("snippet") or ev.get("body") or "",
                "title": ev.get("title") or ev.get("subject", ""),
                "subject": ev.get("subject", ""),
                "link": ev.get("link", ""),
                "date": ev.get("date", ""),
                "message_id": ev.get("message_id", ""),
                "metadata": ev,
            }
        )

    for ev in drive_evidencias:
        evidence_raw.append(
            {
                "source": "drive",
                "content": ev.get("content") or ev.get("title", ""),
                "title": ev.get("title", ""),
                "link": ev.get("link", ""),
                "date": ev.get("date", ""),
                "file_id": ev.get("file_id", ""),
                "metadata": ev,
            }
        )

    for ev in calendar_evidencias:
        evidence_raw.append(
            {
                "source": "calendar",
                "content": ev.get("content") or ev.get("title", ""),
                "title": ev.get("title", ""),
                "link": ev.get("link", ""),
                "date": ev.get("date", ""),
                "event_id": ev.get("event_id", ""),
                "metadata": ev,
            }
        )

    for ev in local_evidence:
        evidence_raw.append(
            {
                "source": "local_file",
                "content": ev.get("text") or ev.get("content") or "",
                "title": ev.get("filename", ""),
                "filename": ev.get("filename", ""),
                "link": ev.get("link", ""),
                "file_id": str(ev.get("file_id", "")),
                "metadata": ev,
            }
        )

    await logger.ainfo(
        "evidence_orchestrator_done",
        email_count=len(email_evidencias),
        drive_count=len(drive_evidencias),
        calendar_count=len(calendar_evidencias),
        local_count=len(local_evidence),
        total_raw=len(evidence_raw),
    )

    return {
        **state,
        "evidence_raw": evidence_raw,
        "current_phase": "evidence_orchestrator",
    }
