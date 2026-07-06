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


def _deduplicate(evidence_list: list[dict]) -> list[dict]:
    """Remove duplicate evidence items by content hash."""
    seen: set[str] = set()
    result: list[dict] = []
    for ev in evidence_list:
        h = _content_hash(ev)
        if h not in seen:
            seen.add(h)
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
