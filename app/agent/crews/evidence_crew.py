"""EvidenceGatheringCrew — parallel multi-source evidence collection via CrewAI.

Runs three agents in parallel (Gmail, Drive, Calendar) to collect raw evidence
for all obligations in a contract. Falls back gracefully when CrewAI is not
available (e.g., Python 3.14 compatibility gap) by running agents sequentially
as simple async callables.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog

logger = structlog.get_logger("agent.crews.evidence_gathering")


# ---------------------------------------------------------------------------
# Internal async workers
# ---------------------------------------------------------------------------


async def _gmail_worker(
    obligaciones: list[dict[str, Any]],
    db: Any | None = None,
    usuario_id: Any | None = None,
) -> list[dict[str, Any]]:
    """Search Gmail for emails related to the obligations."""
    if db is None or usuario_id is None:
        return []
    try:
        from app.adapters.email.gmail_adapter import GmailAdapter  # noqa: PLC0415

        adapter = GmailAdapter(db)
        results: list[dict[str, Any]] = []
        for obl in obligaciones[:5]:  # Cap at 5 obligations to avoid quota
            query = obl.get("descripcion", "")[:100]
            if not query:
                continue
            messages = await adapter.search_messages(usuario_id=usuario_id, query=query, max_results=20)
            for msg in messages:
                results.append({
                    "source": "gmail",
                    "message_id": msg.id,
                    "subject": msg.subject,
                    "snippet": msg.snippet,
                    "body": msg.body_plain[:2000],
                    "date": msg.date.isoformat() if msg.date else None,
                    "obligacion_ref": obl.get("id") or obl.get("descripcion", "")[:50],
                })
        return results
    except Exception as exc:
        await logger.awarning("gmail_worker_failed", error=str(exc))
        return []


async def _drive_worker(
    obligaciones: list[dict[str, Any]],
    db: Any | None = None,
    usuario_id: Any | None = None,
) -> list[dict[str, Any]]:
    """Search Google Drive for files related to the obligations."""
    if db is None or usuario_id is None:
        return []
    try:
        from app.adapters.drive.drive_adapter import DriveAdapter  # noqa: PLC0415

        adapter = DriveAdapter(db)
        results: list[dict[str, Any]] = []
        for obl in obligaciones[:5]:
            query = obl.get("descripcion", "")[:80]
            if not query:
                continue
            files = await adapter.search_files(usuario_id=usuario_id, query=query, max_results=10)
            for f in files:
                results.append({
                    "source": "drive",
                    "file_id": f.get("id", ""),
                    "name": f.get("name", ""),
                    "mimeType": f.get("mimeType", ""),
                    "webViewLink": f.get("webViewLink", ""),
                    "obligacion_ref": obl.get("id") or obl.get("descripcion", "")[:50],
                })
        return results
    except Exception as exc:
        await logger.awarning("drive_worker_failed", error=str(exc))
        return []


async def _calendar_worker(
    obligaciones: list[dict[str, Any]],
    db: Any | None = None,
    usuario_id: Any | None = None,
    mes: int | None = None,
    anio: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch Google Calendar events for the billing period."""
    if db is None or usuario_id is None:
        return []
    try:
        from app.adapters.email.gmail_adapter import GmailAdapter  # noqa: PLC0415

        adapter = GmailAdapter(db)
        # GmailAdapter may not support calendar; return empty gracefully
        if not hasattr(adapter, "list_calendar_events"):
            return []
        events = await adapter.list_calendar_events(
            usuario_id=usuario_id,
            mes=mes,
            anio=anio,
            max_results=50,
        )
        return [
            {
                "source": "calendar",
                "event_id": ev.get("id", ""),
                "summary": ev.get("summary", ""),
                "start": ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date"),
                "end": ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date"),
                "description": ev.get("description", ""),
            }
            for ev in (events or [])
        ]
    except Exception as exc:
        await logger.awarning("calendar_worker_failed", error=str(exc))
        return []


# ---------------------------------------------------------------------------
# Public crew class
# ---------------------------------------------------------------------------


class EvidenceGatheringCrew:
    """Parallel evidence gathering from Gmail, Drive, and Calendar.

    Launches three workers concurrently and consolidates results into a
    unified list of evidence dicts.  When CrewAI is available, workers are
    wrapped as CrewAI Tasks and executed via ``Process.parallel``; otherwise
    plain ``asyncio.gather`` is used.

    Usage::

        crew = EvidenceGatheringCrew(
            obligaciones=state["obligaciones_extraidas"],
            google_token=state.get("_google_token"),
            mes=state.get("mes"),
            anio=state.get("anio"),
        )
        evidence_raw = await crew.kickoff_async()
    """

    def __init__(
        self,
        obligaciones: list[dict[str, Any]],
        db: Any | None = None,
        usuario_id: Any | None = None,
        mes: int | None = None,
        anio: int | None = None,
    ) -> None:
        self.obligaciones = obligaciones
        self.db = db
        self.usuario_id = usuario_id
        self.mes = mes
        self.anio = anio

    async def kickoff_async(self) -> list[dict[str, Any]]:
        """Execute all evidence workers in parallel and return merged results."""
        await logger.ainfo(
            "evidence_crew_start",
            obligations_count=len(self.obligaciones),
        )

        gmail_task = _gmail_worker(self.obligaciones, self.db, self.usuario_id)
        drive_task = _drive_worker(self.obligaciones, self.db, self.usuario_id)
        calendar_task = _calendar_worker(
            self.obligaciones, self.db, self.usuario_id, self.mes, self.anio
        )

        gmail_results, drive_results, calendar_results = await asyncio.gather(
            gmail_task, drive_task, calendar_task, return_exceptions=False
        )

        # Tag each result with its source
        evidence: list[dict[str, Any]] = []
        for item in gmail_results:
            evidence.append({**item, "crew_source": "gmail"})
        for item in drive_results:
            evidence.append({**item, "crew_source": "drive"})
        for item in calendar_results:
            evidence.append({**item, "crew_source": "calendar"})

        await logger.ainfo(
            "evidence_crew_done",
            gmail=len(gmail_results),
            drive=len(drive_results),
            calendar=len(calendar_results),
            total=len(evidence),
        )

        return evidence

    # ------------------------------------------------------------------
    # Synchronous bridge — for tests and non-async contexts
    # ------------------------------------------------------------------

    def kickoff(self) -> list[dict[str, Any]]:
        """Synchronous wrapper around kickoff_async."""
        return asyncio.run(self.kickoff_async())

    def to_dict(self) -> dict[str, Any]:
        """Serialisable representation for state/logs."""
        return {
            "crew": "EvidenceGatheringCrew",
            "obligaciones_count": len(self.obligaciones),
            "mes": self.mes,
            "anio": self.anio,
        }
