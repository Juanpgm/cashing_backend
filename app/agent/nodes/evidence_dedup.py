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


def _deduplicate(evidence_list: list[dict]) -> list[dict]:
    """Remove duplicate evidence items.

    An item matches a PRIOR item (and is dropped) if EITHER signal matches:
    - its external id (message_id/file_id/event_id) — the same message
      re-fetched with an updated snippet still collapses to one; OR
    - its content hash, when content is non-empty — e.g. the identical
      filename surfacing from two different Google/Microsoft file ids for
      what's really the same uploaded document (cross-provider duplicate).

    An item with EMPTY content is only matched via its id, never via content
    hash — `_content_hash("")` is the SAME constant hash for every empty
    string, so genuinely different messages with no extractable body text
    (root cause #6, evidencias/discovery-fix) no longer collapse into one
    just because both happen to be empty. An item with neither an id nor
    non-empty content is always kept — nothing proves two such items are the
    same evidence.
    """
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    result: list[dict] = []
    for ev in evidence_list:
        id_key = _external_id_key(ev)
        if id_key is not None and id_key in seen_ids:
            continue

        content = ev.get("content") or ev.get("text") or ""
        content_hash = _content_hash(ev) if content else None
        if content_hash is not None and content_hash in seen_hashes:
            continue

        if id_key is None and content_hash is None:
            result.append(ev)
            continue

        if id_key is not None:
            seen_ids.add(id_key)
        if content_hash is not None:
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
        ob_id: _deduplicate(ev_list)
        for ob_id, ev_list in matched_evidence.items()
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
