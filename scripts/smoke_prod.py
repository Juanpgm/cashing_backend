# ruff: noqa: T201 — smoke CLI: los print son la salida esperada.
"""Read-only production smoke test for the cashing backend.

SAFE AGAINST PROD: every request here is a GET (plus one auth login POST that
creates NO business data). No contrato, cuenta de cobro, evidencia, documento or
pago is ever created, mutated or deleted. Run it after a Railway deploy to
confirm the live stack is healthy before anyone touches real data.

Config via env vars (NEVER hardcode credentials):
  SMOKE_BASE_URL   base URL, e.g. https://cashin-api-production.up.railway.app
                   (default: that same prod URL)
  SMOKE_USER       email of a PRE-EXISTING dedicated prod test account
  SMOKE_PASS       its password
  SMOKE_CEDULA     cédula used for the read-only SECOP consulta
                   (default: 1016019452, the known test cédula with contratos)

If SMOKE_USER/SMOKE_PASS are unset, only the unauthenticated health checks run
and the authed read-only checks are skipped with a printed notice.

Run:
  SMOKE_USER=... SMOKE_PASS=... uv run python scripts/smoke_prod.py

Exit code 0 if every non-WARN step passed, 1 otherwise. A WARN (e.g. an LLM
provider degraded, or SECOP's upstream datos.gov.co returning 5xx) is reported
but never fails the run — those are external dependencies, not our stack.
"""

from __future__ import annotations

import os
import sys

import httpx

DEFAULT_BASE_URL = "https://cashin-api-production.up.railway.app"
DEFAULT_CEDULA = "1016019452"

# Result counters, mutated by _record.
_failures = 0
_warnings = 0


def _record(status: str, name: str, detail: str = "") -> None:
    global _failures, _warnings
    if status == "FAIL":
        _failures += 1
    elif status == "WARN":
        _warnings += 1
    line = f"[{status:4}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def check_health(client: httpx.Client) -> None:
    """(a) GET /health → 200 with an `environment` field."""
    try:
        r = client.get("/health")
        if r.status_code == 200 and "environment" in r.json():
            _record("PASS", "GET /health", f"environment={r.json().get('environment')}")
        else:
            _record("FAIL", "GET /health", f"status={r.status_code} body={r.text[:200]}")
    except Exception as exc:  # noqa: BLE001 — smoke script surfaces any failure
        _record("FAIL", "GET /health", str(exc))


def check_llm_health(client: httpx.Client) -> None:
    """(b) GET /api/v1/health/llm → 200, status in {ok, degraded}.

    A `degraded`/`error` status or a transport error is a WARN, not a hard fail:
    an LLM provider blip must not red-flag the whole stack.
    """
    try:
        r = client.get("/api/v1/health/llm")
        if r.status_code != 200:
            _record("WARN", "GET /health/llm", f"status={r.status_code} body={r.text[:200]}")
            return
        status = r.json().get("status")
        if status in ("ok", "degraded"):
            _record("PASS", "GET /health/llm", f"status={status}")
        else:
            _record("WARN", "GET /health/llm", f"status={status}")
    except Exception as exc:  # noqa: BLE001
        _record("WARN", "GET /health/llm", str(exc))


def login(client: httpx.Client, user: str, password: str) -> str | None:
    """(c) POST /api/v1/auth/login → 200, return the access token."""
    try:
        r = client.post("/api/v1/auth/login", json={"email": user, "password": password})
        if r.status_code == 200 and r.json().get("access_token"):
            _record("PASS", "POST /auth/login", f"user={user}")
            return str(r.json()["access_token"])
        _record("FAIL", "POST /auth/login", f"status={r.status_code} body={r.text[:200]}")
        return None
    except Exception as exc:  # noqa: BLE001
        _record("FAIL", "POST /auth/login", str(exc))
        return None


def check_authed_get(
    client: httpx.Client,
    name: str,
    path: str,
    token: str,
    *,
    params: dict[str, str] | None = None,
    external: bool = False,
) -> None:
    """A read-only authed GET asserting a 2xx. `external=True` downgrades upstream
    5xx (e.g. SECOP's datos.gov.co) to a WARN instead of a FAIL."""
    try:
        r = client.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
        if 200 <= r.status_code < 300:
            _record("PASS", name, f"status={r.status_code}")
        elif external and r.status_code >= 500:
            _record("WARN", name, f"upstream status={r.status_code}")
        else:
            _record("FAIL", name, f"status={r.status_code} body={r.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        _record("WARN" if external else "FAIL", name, str(exc))


def main() -> int:
    base_url = os.getenv("SMOKE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    user = os.getenv("SMOKE_USER")
    password = os.getenv("SMOKE_PASS")
    cedula = os.getenv("SMOKE_CEDULA", DEFAULT_CEDULA)

    print(f"Smoke test (READ-ONLY) against {base_url}\n")

    with httpx.Client(base_url=base_url, timeout=30.0, follow_redirects=True) as client:
        check_health(client)
        check_llm_health(client)

        if not user or not password:
            _record("SKIP", "authed reads", "SMOKE_USER/SMOKE_PASS unset — authed checks skipped")
        else:
            token = login(client, user, password)
            if token:
                # Read-only listings — never create business data.
                check_authed_get(client, "GET /cuentas-cobro/", "/api/v1/cuentas-cobro/", token)
                check_authed_get(client, "GET /contratos/", "/api/v1/contratos/", token)
                check_authed_get(client, "GET /dashboard", "/api/v1/dashboard", token)
                check_authed_get(client, "GET /creditos/balance", "/api/v1/creditos/balance", token)
                # SECOP hits datos.gov.co upstream — tolerate its 5xx as WARN.
                check_authed_get(
                    client,
                    "GET /secop/consulta",
                    "/api/v1/secop/consulta",
                    token,
                    params={"cedula": cedula},
                    external=True,
                )

    print(f"\n{_failures} failure(s), {_warnings} warning(s).")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
