"""Shared text-matching utilities (accent normalization + keyword scoring)."""

from __future__ import annotations

import difflib
import unicodedata
from collections.abc import Iterable
from decimal import Decimal


def strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalize(text: str | None) -> str:
    if not text:
        return ""
    return strip_accents(text).lower()


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


def keyword_score(haystacks: Iterable[str | None], keywords: list[str]) -> Decimal:
    """Score 0.0-1.0 = fraction of keywords found in any haystack (normalised)."""
    if not keywords:
        return Decimal("0.000")
    blob = " ".join(normalize(h) for h in haystacks if h)
    if not blob:
        return Decimal("0.000")
    norm_kws = [normalize(k).strip() for k in keywords if k and k.strip()]
    if not norm_kws:
        return Decimal("0.000")
    hits = sum(1 for k in norm_kws if k and k in blob)
    raw = hits / len(norm_kws)
    return Decimal(f"{raw:.3f}")
