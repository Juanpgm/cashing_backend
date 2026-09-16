"""Semantic query-term expansion node (evidencias/discovery-fix WU7).

`expand_search_terms` asks ONE LLM call for 4-8 search phrases per obligación
(synonyms, expected deliverable names, likely counterpart names, filename
variants) so the Gmail/Drive/Calendar query builders can find evidence that
never mentions the contract number or the obligación's own literal wording.
Falls back to the deterministic `_extract_keywords` per obligación when the
LLM is unavailable, a fake/test double without a usable response, or returns
malformed output — this must never fail the discovery run.
"""

from __future__ import annotations

import json
import re

import structlog

from app.agent.prompts.contract_terms import contract_header
from app.agent.prompts.email_evidence import _extract_keywords
from app.agent.prompts.query_expansion import QUERY_EXPANSION_SYSTEM_PROMPT, build_query_expansion_prompt
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.query_expansion")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_PHRASES_PER_OBLIGACION = 8


def _obligacion_id(ob: dict, index: int) -> str:
    return str(ob.get("id") or index)


def _deterministic_terms(obligaciones: list[dict]) -> dict[str, list[str]]:
    """Fallback: reuse the existing keyword extractor per obligación."""
    return {
        _obligacion_id(ob, i): _extract_keywords(str(ob.get("descripcion") or "")) for i, ob in enumerate(obligaciones)
    }


def _parse_expansion_response(raw: str, valid_ids: set[str]) -> dict[str, list[str]] | None:
    """Parse `{"<id>": ["frase", ...], ...}`. Returns None on ANY parse/shape
    failure so the caller falls back to deterministic terms entirely — a
    partially-malformed response is not trusted."""
    match = _JSON_RE.search(raw or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    result: dict[str, list[str]] = {}
    for ob_id, phrases in data.items():
        if ob_id not in valid_ids or not isinstance(phrases, list):
            continue
        cleaned = [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
        if cleaned:
            result[ob_id] = cleaned[:_MAX_PHRASES_PER_OBLIGACION]
    return result or None


async def expand_search_terms(contexto: dict | None, obligaciones: list[dict], llm) -> dict[str, list[str]]:
    """Return `{obligacion_id: [search phrase, ...]}` — one LLM call total.

    `llm=None` (no provider available) skips the call entirely and returns
    the deterministic fallback. Any obligación the LLM's response doesn't
    cover (missing key, or the whole response is unparseable) still gets its
    deterministic fallback terms — this never returns an empty phrase list
    for an obligación that has a description.
    """
    if not obligaciones:
        return {}

    fallback = _deterministic_terms(obligaciones)
    if llm is None:
        return fallback

    header = contract_header(contexto)
    prompt = build_query_expansion_prompt(header, obligaciones)
    valid_ids = set(fallback.keys())

    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=QUERY_EXPANSION_SYSTEM_PROMPT),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.3,
            max_tokens=1200,
            reasoning_effort="low",
        )
        parsed = _parse_expansion_response(resp.content, valid_ids)
    except Exception as exc:
        logger.warning("query_expansion_llm_failed", error=str(exc), n_obligaciones=len(obligaciones))
        return fallback

    if parsed is None:
        return fallback

    # Merge: any obligación the LLM didn't answer for keeps its deterministic
    # fallback instead of being silently left with zero search phrases.
    merged = dict(fallback)
    merged.update(parsed)
    return merged
