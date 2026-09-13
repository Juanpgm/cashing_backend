"""Unit tests for `app.core.exceptions.domain_to_http`.

Covers the MRO-walk status lookup: `domain_to_http` must resolve the HTTP
status for `type(exc)` first, then walk `type(exc).__mro__` for the first
mapped ancestor, falling back to 500 only when nothing in the MRO is
registered in `EXCEPTION_STATUS_MAP`.
"""

from __future__ import annotations

import pytest
from app.core.exceptions import (
    EXCEPTION_STATUS_MAP,
    AlreadyExistsError,
    ChecklistLinkError,
    DomainError,
    ExternalServiceError,
    ForbiddenError,
    InsufficientCreditsError,
    InviteRequiredError,
    NotFoundError,
    RateLimitExceededError,
    UnauthorizedError,
    ValidationError,
    domain_to_http,
)
from fastapi import status


def _instantiate(exc_cls: type[DomainError]) -> DomainError:
    """Build a valid instance of every class registered in
    `EXCEPTION_STATUS_MAP`. Most accept no args; `ChecklistLinkError` needs
    its required constructor args.
    """
    if exc_cls is ChecklistLinkError:
        return ChecklistLinkError(requisito_codigo="REQ-1", reason="test reason")
    return exc_cls()


# --- Regression net: every existing mapping is unchanged ---


@pytest.mark.parametrize("exc_cls", list(EXCEPTION_STATUS_MAP.keys()), ids=lambda c: c.__name__)
def test_every_registered_exception_maps_to_its_exact_status(exc_cls: type[DomainError]) -> None:
    exc = _instantiate(exc_cls)
    http_exc = domain_to_http(exc)
    assert http_exc.status_code == EXCEPTION_STATUS_MAP[exc_cls]


def test_regression_exact_status_codes_pinned() -> None:
    """Explicit enumeration (not just the parametrized loop) so a future edit
    to EXCEPTION_STATUS_MAP that changes an existing value is caught even if
    the parametrized test above is modified to read from the map itself."""
    assert domain_to_http(NotFoundError()).status_code == status.HTTP_404_NOT_FOUND
    assert domain_to_http(AlreadyExistsError()).status_code == status.HTTP_409_CONFLICT
    assert domain_to_http(ValidationError()).status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert domain_to_http(InsufficientCreditsError()).status_code == status.HTTP_402_PAYMENT_REQUIRED
    assert domain_to_http(UnauthorizedError()).status_code == status.HTTP_401_UNAUTHORIZED
    assert domain_to_http(ForbiddenError()).status_code == status.HTTP_403_FORBIDDEN
    assert domain_to_http(RateLimitExceededError()).status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert domain_to_http(ExternalServiceError()).status_code == status.HTTP_502_BAD_GATEWAY
    assert domain_to_http(InviteRequiredError()).status_code == status.HTTP_403_FORBIDDEN
    assert (
        domain_to_http(ChecklistLinkError(requisito_codigo="REQ-1", reason="x")).status_code
        == status.HTTP_502_BAD_GATEWAY
    )


# --- MRO walk: unmapped subclass inherits its mapped parent's status ---


class _CustomValidationError(ValidationError):
    """Test-only subclass, deliberately NOT registered in EXCEPTION_STATUS_MAP."""


def test_unmapped_subclass_of_mapped_exception_inherits_parent_status() -> None:
    assert _CustomValidationError not in EXCEPTION_STATUS_MAP
    exc = _CustomValidationError("custom validation failure")
    http_exc = domain_to_http(exc)
    assert http_exc.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class _CustomNotFoundError(NotFoundError):
    """Another unmapped subclass, of a different mapped parent, to make sure
    the walk isn't accidentally hardcoded to ValidationError."""


def test_unmapped_subclass_of_notfound_inherits_404() -> None:
    assert _CustomNotFoundError not in EXCEPTION_STATUS_MAP
    exc = _CustomNotFoundError(resource="Widget")
    http_exc = domain_to_http(exc)
    assert http_exc.status_code == status.HTTP_404_NOT_FOUND


# --- Fallback: no mapped ancestor anywhere in the MRO still 500s ---


class _TrulyUnknownError(DomainError):
    """Unmapped, and its only ancestor (DomainError) is also unmapped."""


def test_exception_with_no_mapped_ancestor_falls_back_to_500() -> None:
    assert _TrulyUnknownError not in EXCEPTION_STATUS_MAP
    assert DomainError not in EXCEPTION_STATUS_MAP
    exc = _TrulyUnknownError("unknown failure")
    http_exc = domain_to_http(exc)
    assert http_exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


def test_bare_domain_error_falls_back_to_500() -> None:
    http_exc = domain_to_http(DomainError("bare error"))
    assert http_exc.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR


# --- Multiple inheritance: most-specific-first MRO order decides the winner ---


class _MultiParentError(NotFoundError, ForbiddenError):
    """Inherits from two mapped classes with DIFFERENT statuses (404 vs 403).

    Python's C3 linearization puts `NotFoundError` before `ForbiddenError` in
    `_MultiParentError.__mro__` because `NotFoundError` is listed first in the
    base class tuple, so the MRO walk must pick NotFoundError's 404 — the
    left-most/most-specific-before-less-specific base wins, not simply "any
    mapped ancestor".
    """


def test_multiple_inheritance_picks_leftmost_mro_mapped_status() -> None:
    mro_classes = _MultiParentError.__mro__
    assert mro_classes.index(NotFoundError) < mro_classes.index(ForbiddenError)

    exc = _MultiParentError()
    http_exc = domain_to_http(exc)
    assert http_exc.status_code == status.HTTP_404_NOT_FOUND


# --- `code` comes from the instance, never from the MRO walk ---


def test_code_attribute_is_read_from_instance_not_from_mro_walk() -> None:
    """`domain_to_http` only resolves HTTP status via the MRO walk. The
    domain `code` (used for user-facing/frontend messages) must stay whatever
    was set on the exception instance at construction time, completely
    unaffected by the status-mapping walk."""
    with_code = AlreadyExistsError(resource="Cuenta", field="mes", code="CUENTA_MES_DUPLICADA")
    without_code = AlreadyExistsError(resource="Cuenta", field="mes")

    # Both map to the SAME status (both are exactly AlreadyExistsError)...
    assert domain_to_http(with_code).status_code == status.HTTP_409_CONFLICT
    assert domain_to_http(without_code).status_code == status.HTTP_409_CONFLICT

    # ...but `code` differs per instance, proving it isn't derived from the
    # class/MRO lookup that decided the status.
    assert with_code.code == "CUENTA_MES_DUPLICADA"
    assert without_code.code is None

    # An unmapped subclass inherits its parent's status via the MRO walk,
    # while still carrying its own independently-set instance `code`.
    subclass_exc = _CustomValidationError("bad state", code="CUSTOM_CODE")
    assert domain_to_http(subclass_exc).status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    assert subclass_exc.code == "CUSTOM_CODE"
