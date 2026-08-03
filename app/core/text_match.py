"""Shared text-matching utilities (accent normalization + keyword scoring)."""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable
from decimal import Decimal


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(text: str | None) -> str:
    """Accent/case-insensitive, whitespace-collapsed form for tolerant matching.

    Free-text fields (e.g. `Contrato.entidad` from SECOP imports / manual
    entry) vary by stray leading/trailing/doubled spaces across rows that
    represent the same real-world value — collapsing whitespace here (not
    just accents/case) keeps every caller's matching symmetric. Previously
    this only stripped accents/lowercased; `docx_clone_service`/
    `xlsx_clone_service` each layered `" ".join(normalize(text).split())` on
    top locally to get this exact behavior — folded into the shared
    primitive instead of duplicating it a third time.
    """
    if not text:
        return ""
    return " ".join(strip_accents(text).lower().split())


def solo_digitos(text: str | None) -> str:
    """Digits only, with leading zeros stripped — for tolerant cedula/NIT compare.

    Manual entry often adds or drops leading zeros or punctuation, so
    ``solo_digitos("C.C. 01.234.567")`` == ``solo_digitos("1234567")`` == ``"1234567"``.
    """
    if not text:
        return ""
    d = "".join(c for c in text if c.isdigit())
    return d.lstrip("0") or ("0" if d else "")


def _nucleo(text: str | None) -> str:
    """Alphanumeric-only, accent/case-insensitive key — drops spaces, dashes, dots.

    Turns identifiers like ``"CD-045 / 2025"`` and ``"cd0452025"`` into the same key,
    so case, spaces and special characters no longer break equality.
    """
    return "".join(c for c in normalize(text) if c.isalnum())


def similar(a: str | None, b: str | None) -> Decimal:
    """Fuzzy similarity 0.000-1.000 between two identifiers (accent/case/punct-insensitive).

    Both sides are reduced to their alphanumeric core before comparison, then scored
    with :class:`difflib.SequenceMatcher`. Equal cores return 1.000; empty input 0.000.
    Tolerates the minor differences typical of hand-entered contract numbers.
    """
    ka, kb = _nucleo(a), _nucleo(b)
    if not ka or not kb:
        return Decimal("0.000")
    if ka == kb:
        return Decimal("1.000")
    ratio = difflib.SequenceMatcher(None, ka, kb).ratio()
    return Decimal(f"{ratio:.3f}")


def _kw_regex(kw: str) -> re.Pattern[str]:
    """Word-boundary regex for a normalized keyword (blob is already normalize()'d).

    Boundary is asserted only where the keyword's edge character is alphanumeric,
    so hyphen keywords (``"ds-"``, ``"cdp-"``) still match ``"ds-045"``, while short
    keywords like ``"rp"``/``"cc"``/``"cto"``/``"rut"``/``"pila"`` no longer match
    inside longer words (``"cuerpo"``, ``"seccion"``, ``"proyecto"``, ``"bruto"``,
    ``"recopilacion"``). Underscore/dot count as boundaries too. Multi-word keywords
    (already whitespace-collapsed by ``normalize``) match as a verbatim adjacent-token
    phrase since the space between words is itself non-alnum.

    The boundary class is LETTERS only (``[a-z]``), not alphanumeric — a digit
    glued directly to the keyword (no separator) still counts as a boundary, so
    e.g. ``"rut"`` matches ``"rut1140836527.pdf"`` (RUT filenames glue the id
    right after the keyword). Only an adjacent LETTER blocks the match.
    ``normalize()`` already strips accents before this regex runs, so a plain
    ``[a-z]`` class is sufficient — no unicode letter class is needed.
    """
    left = r"(?<![a-z])" if kw[:1].isalnum() else ""
    right = r"(?![a-z])" if kw[-1:].isalnum() else ""
    return re.compile(left + re.escape(kw) + right)


def keyword_hits(haystacks: Iterable[str | None], keywords: list[str]) -> list[str]:
    """Keywords (as given) that match `haystacks` on normalized word boundaries."""
    blob = " ".join(normalize(h) for h in haystacks if h)
    if not blob:
        return []
    hits = []
    for k in keywords:
        norm_k = normalize(k).strip()
        if norm_k and _kw_regex(norm_k).search(blob):
            hits.append(k)
    return hits


def keyword_score(haystacks: Iterable[str | None], keywords: list[str]) -> Decimal:
    """Score 0.0-1.0 = fraction of keywords found in any haystack (normalised, word-boundary)."""
    if not keywords:
        return Decimal("0.000")
    norm_kws = [k for k in keywords if k and normalize(k).strip()]
    if not norm_kws:
        return Decimal("0.000")
    hits = keyword_hits(haystacks, norm_kws)
    raw = len(hits) / len(norm_kws)
    return Decimal(f"{raw:.3f}")
