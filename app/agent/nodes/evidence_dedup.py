"""Evidence deduplication node — SHA-256 hash dedup (Phase 4)."""

from __future__ import annotations

import hashlib

import structlog

from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.evidence_dedup")


def _content_hash(evidence: dict) -> str:
    """Compute SHA-256 of evidence content for deduplication."""
    content = evidence.get("content") or evidence.get("text") or ""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _external_id_key(evidence: dict) -> str | None:
    """`(source, external id)` when the item carries a stable provider id
    (message_id/file_id/event_id) — a much stronger identity signal than
    content, and required to fix root cause #6 (evidencias/discovery-fix):
    `_content_hash("")` is the SAME constant hash for every empty-body
    email, so genuinely different messages with no extractable body text
    used to collapse into one."""
    external_id = evidence.get("message_id") or evidence.get("file_id") or evidence.get("event_id")
    if not external_id:
        return None
    return f"{evidence.get('source') or ''}:{external_id}"


def _file_identity_key(evidence: dict[str, object]) -> tuple[str, object, str] | None:
    """`(name, size, mime)` for a file — the ONLY cross-provider dedup signal.

    The same document uploaded to both Google Drive and OneDrive gets different
    `file_id`s, so id-authoritative dedup alone would keep both. Keying on the
    filename ALONE (what content hashing effectively did, since drive_fetch
    sets `content = f.name`) is far too aggressive: monthly deliverables in this
    domain are genuinely named "Informe mensual.pdf" every single month. Size
    and mime discriminate those apart. Returns None when size is unknown, so a
    missing size never collapses two distinct files.
    """
    if not evidence.get("file_id"):
        return None
    name = str(evidence.get("title") or evidence.get("filename") or "").strip().lower()
    size = evidence.get("size")
    if not name or size is None:
        return None
    return (name, size, str(evidence.get("mime_type") or ""))


def _deduplicate(evidence_list: list[dict]) -> list[dict]:
    """Remove duplicate evidence items. The external id is AUTHORITATIVE.

    An item carrying a provider id (message_id/file_id/event_id) is dropped
    ONLY when that id was already seen — never because its text happens to
    match another item's. Round-2 fix for a confirmed CRITICAL finding: the
    previous version fell through to a content-hash check even when the id was
    brand new, and since

    - `calendar_fetch._event_content` deliberately EXCLUDES the event date,
      every instance of a recurring "Comité de seguimiento" is byte-identical,
    - `drive_fetch` sets `content = f.name`, so same-named monthly documents
      are byte-identical,
    - Spanish acknowledgements are routinely the identical short body
      ("Recibido, gracias."),

    all but the first of each were silently deleted. Dedup runs BEFORE
    filter/matcher/justify (`evidence_discovery_service` reassigns
    `evidence_raw = deduplicated_evidence`), so that pool is the only input the
    rest of the pipeline ever sees — the loss was upstream of everything.

    Content hashing is retained ONLY for items with NO external id, where
    nothing better exists. Empty content never matches (root cause #6:
    `_content_hash("")` is the same constant for every empty body). Genuine
    cross-provider file duplicates are still collapsed, via
    `_file_identity_key`'s `(name, size, mime)` rather than the bare name.
    """
    seen_ids: set[str] = set()
    seen_files: set[tuple[str, object, str]] = set()
    seen_hashes: set[str] = set()
    result: list[dict] = []
    for ev in evidence_list:
        id_key = _external_id_key(ev)

        if id_key is not None:
            if id_key in seen_ids:
                continue
            file_key = _file_identity_key(ev)
            if file_key is not None:
                if file_key in seen_files:
                    continue
                seen_files.add(file_key)
            seen_ids.add(id_key)
            result.append(ev)
            continue

        # No external id — content is the only identity signal available.
        content = ev.get("content") or ev.get("text") or ""
        if not content:
            result.append(ev)
            continue
        content_hash = _content_hash(ev)
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        result.append(ev)
    return result


async def evidence_dedup_node(state: AgentState) -> AgentState:
    """Deduplicate evidence using SHA-256 content hashing.

    Reads: evidence_raw, matched_evidence
    Writes: deduplicated_evidence, current_phase
    """
    evidence_raw: list[dict] = state.get("evidence_raw") or []
    matched_evidence: dict[str, list[dict]] = state.get("matched_evidence") or {}

    # Deduplicate the global evidence pool
    deduped_raw = _deduplicate(evidence_raw)

    # Deduplicate per-obligation evidence in matched_evidence
    deduped_matched: dict[str, list[dict]] = {
        ob_id: _deduplicate(ev_list) for ob_id, ev_list in matched_evidence.items()
    }

    # Final deduplicated_evidence: flat list from matched (preserving per-obligation dedup)
    all_matched = []
    seen_global: set[str] = set()
    for ob_id, ev_list in deduped_matched.items():
        for ev in ev_list:
            h = _content_hash(ev)
            if h not in seen_global:
                seen_global.add(h)
                all_matched.append({**ev, "_matched_to": ob_id})

    # Use matched deduped if available, else fall back to raw deduped
    deduplicated = all_matched if all_matched else deduped_raw

    removed = len(evidence_raw) - len(deduped_raw)
    await logger.ainfo(
        "evidence_dedup_done",
        raw_count=len(evidence_raw),
        deduped_count=len(deduped_raw),
        removed=removed,
        final_count=len(deduplicated),
    )

    return {
        **state,
        "deduplicated_evidence": deduplicated,
        # Also update matched_evidence with deduped version
        "matched_evidence": deduped_matched,
        "current_phase": "evidence_dedup",
    }
