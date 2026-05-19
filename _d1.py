"""Minimal Cloudflare D1 REST client for LitFeed.

LitFeed talks to D1 from two places:

* the Cloudflare Worker, which uses the native ``env.DB`` binding (not this
  module) — see ``worker/index.js``.
* ``main.py`` running on a GitHub Actions runner, which has no edge binding
  and must go through the HTTPS API. That's what this module is for.

Endpoint:
    POST https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query

Auth: Bearer token with the ``D1:Edit`` permission on this database.

Configured via three env vars:
    CF_ACCOUNT_ID       Cloudflare account id (Account Home → right sidebar)
    CF_D1_DATABASE_ID   D1 database id (printed by ``wrangler d1 create``)
    CF_D1_API_TOKEN     API token (dash.cloudflare.com → My Profile → API Tokens)

Functions:
    is_configured()                 → True iff all three env vars are present
    query(sql, params=None)         → list[dict] of rows for a SELECT, or a
                                       dict with meta for a write
    batch(statements)               → atomic multi-statement run; statements is
                                       a list of (sql, params) tuples

The batch endpoint is critical for ``last_batch`` (we always wholly replace
that table per daily run) and for trimming ``sent_ids`` to ``MAX_SENT_IDS``.
"""

from __future__ import annotations

import os
import sys
from typing import Any

import requests


class D1Error(RuntimeError):
    """Raised on any non-success response from the D1 REST API."""


_API_BASE = "https://api.cloudflare.com/client/v4"
_TIMEOUT_SECONDS = 20


def _env() -> tuple[str, str, str] | None:
    account = os.environ.get("CF_ACCOUNT_ID", "").strip()
    db = os.environ.get("CF_D1_DATABASE_ID", "").strip()
    token = os.environ.get("CF_D1_API_TOKEN", "").strip()
    if not (account and db and token):
        return None
    return account, db, token


def is_configured() -> bool:
    """True iff CF_ACCOUNT_ID / CF_D1_DATABASE_ID / CF_D1_API_TOKEN are all set."""
    return _env() is not None


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    env = _env()
    if env is None:
        raise D1Error(
            "D1 not configured: set CF_ACCOUNT_ID, CF_D1_DATABASE_ID, "
            "CF_D1_API_TOKEN env vars."
        )
    account, db, token = env
    url = f"{_API_BASE}/accounts/{account}/d1/database/{db}/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=_TIMEOUT_SECONDS)
    except requests.RequestException as exc:
        raise D1Error(f"D1 HTTP error: {exc}") from exc

    try:
        data = resp.json()
    except ValueError as exc:
        raise D1Error(f"D1 returned non-JSON (status {resp.status_code}): {resp.text[:300]}") from exc

    if not resp.ok or not data.get("success", False):
        errors = data.get("errors") or [{"message": resp.text[:300]}]
        msg = "; ".join(str(e.get("message", e)) for e in errors)
        raise D1Error(f"D1 API error (status {resp.status_code}): {msg}")
    return data


def query(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Run a single SQL statement. Returns the result rows (empty for writes).

    ``params`` are positional ``?`` placeholders. Pass a list — strings, ints,
    floats, None. JSON-encode complex values at the call site.
    """
    body: dict[str, Any] = {"sql": sql}
    if params:
        body["params"] = list(params)
    data = _post("query", body)
    # D1 returns {"result": [{"results": [...rows...], "success": bool, "meta": {...}}], ...}
    results = data.get("result", [])
    if not results:
        return []
    first = results[0]
    if not first.get("success", True):
        raise D1Error(f"D1 statement failed: {first}")
    return first.get("results", []) or []


def batch(statements: list[tuple[str, list[Any] | None]]) -> list[list[dict[str, Any]]]:
    """Run multiple SQL statements as a single batch. Returns per-statement rows.

    ``statements`` is a list of (sql, params) tuples. The REST endpoint
    expects a ``{"batch": [{sql, params}, ...]}`` envelope (see
    https://developers.cloudflare.com/api/operations/cloudflare-d1-query-database).
    Statements run sequentially within the batch.
    """
    if not statements:
        return []
    items: list[dict[str, Any]] = []
    for sql, params in statements:
        item: dict[str, Any] = {"sql": sql}
        if params:
            item["params"] = list(params)
        items.append(item)
    data = _post("query", {"batch": items})
    out: list[list[dict[str, Any]]] = []
    for item in data.get("result", []):
        if not item.get("success", True):
            raise D1Error(f"D1 batch statement failed: {item}")
        out.append(item.get("results", []) or [])
    return out


def health_check() -> bool:
    """Quick connectivity probe. Returns True on success, False otherwise.

    Used by ``state_store`` to decide whether to attempt D1 reads/writes when
    parallel-write mode is on.
    """
    try:
        query("SELECT 1 AS ok")
        return True
    except D1Error as exc:
        print(f"[_d1] health check failed: {exc}", file=sys.stderr)
        return False
