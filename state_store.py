"""LitFeed state store: Cloudflare D1 backend.

All runtime state lives in Cloudflare D1 (SQLite at the edge) — votes,
reading_log, sent_ids, last_batch, and the Telegram offset (`kv`). The only
thing left in the repo is `config.json`, which holds the user-editable
``categories`` list and nothing else.

D1 is mandatory: the three env vars below must be set or load/save will
raise :class:`_d1.D1Error` on the first call. This is intentional — there's
no JSON fallback to silently diverge from.

Required env vars (CI and local)::

    CF_ACCOUNT_ID       Cloudflare account id
    CF_D1_DATABASE_ID   D1 database id
    CF_D1_API_TOKEN     API token with D1:Edit

Public surface
--------------
Legacy ``load_*`` / ``save_*`` calls in :mod:`main` keep their dict shapes::

    load_config()       -> {"categories": [...], "last_update_id": int, "sent_ids": [...]}
    load_votes()        -> {"liked": [...], "disliked": [...], "last_batch": {...}}
    load_reading_log()  -> {"papers": {...}}

Narrow mutators (preferred at new call sites) — each is a single-row
``INSERT … ON CONFLICT DO UPDATE`` so two concurrent runs can't clobber each
other::

    record_vote(key, text, bucket, ts)
    upsert_paper_log(key, **fields)
    replace_last_batch(batch)
    append_sent_ids(keys, sent_ts)
    set_last_update_id(n)
    set_categories(categories)         # writes config.json (the only file write)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

import _d1

_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = _ROOT / "config.json"

MAX_SENT_IDS = 500
MAX_VOTES_PER_SIDE = 250

DEFAULT_CATEGORIES: list[str] = []  # populated by main.py at import time


def set_default_categories(categories: Iterable[str]) -> None:
    """Used by :mod:`main` to register the bootstrap category list."""
    global DEFAULT_CATEGORIES
    DEFAULT_CATEGORIES = list(categories)


# ---------------------------------------------------------------- config.json (categories only)


def _load_config_file() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with CONFIG_PATH.open() as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[state_store] failed to read config.json: {exc}", file=sys.stderr)
        return {}


def _dump_config_file(categories: list[str]) -> None:
    """Atomic-ish write: only field persisted is ``categories``."""
    with CONFIG_PATH.open("w") as f:
        json.dump({"categories": list(categories)}, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------- read APIs


def load_config() -> dict[str, Any]:
    """Return ``{categories, last_update_id, sent_ids}``.

    ``categories`` comes from ``config.json`` (or :data:`DEFAULT_CATEGORIES`
    when the file hasn't been seeded). The other two come from D1.
    """
    file_cfg = _load_config_file()
    categories = file_cfg.get("categories") or list(DEFAULT_CATEGORIES)
    return {
        "categories": list(categories),
        "last_update_id": _d1_get_last_update_id(),
        "sent_ids": _d1_get_sent_ids(),
    }


def load_votes() -> dict[str, Any]:
    """Return ``{liked, disliked, last_batch}`` — all read from D1."""
    vote_rows = _d1.query("SELECT paper_key, bucket, text, ts FROM votes ORDER BY ts ASC")
    liked = [
        {"key": r["paper_key"], "text": r["text"], "ts": r["ts"]}
        for r in vote_rows
        if r.get("bucket") == "liked"
    ]
    disliked = [
        {"key": r["paper_key"], "text": r["text"], "ts": r["ts"]}
        for r in vote_rows
        if r.get("bucket") == "disliked"
    ]

    batch_rows = _d1.query(
        "SELECT position, paper_key, text, score FROM last_batch ORDER BY position ASC"
    )
    last_batch: dict[str, dict[str, Any]] = {}
    for r in batch_rows:
        entry: dict[str, Any] = {"key": r["paper_key"], "text": r.get("text")}
        if r.get("score") is not None:
            entry["score"] = r["score"]
        last_batch[str(r["position"])] = entry

    return {"liked": liked, "disliked": disliked, "last_batch": last_batch}


def load_reading_log() -> dict[str, Any]:
    """Return ``{papers: {key: entry, ...}}`` — all read from D1."""
    rows = _d1.query(
        "SELECT paper_key, title, url, text, categories, score, status, status_ts, "
        "sent_ts, created_ts, grok_summary FROM reading_log"
    )
    papers: dict[str, Any] = {}
    for r in rows:
        entry: dict[str, Any] = {
            "key": r["paper_key"],
            "created_ts": r.get("created_ts") or "",
        }
        for col in ("title", "url", "text", "status", "status_ts", "sent_ts"):
            value = r.get(col)
            if value is not None:
                entry[col] = value
        if r.get("score") is not None:
            entry["score"] = r["score"]
        if r.get("categories"):
            try:
                entry["categories"] = json.loads(r["categories"])
            except (TypeError, ValueError):
                pass
        if r.get("grok_summary"):
            try:
                entry["grok_summary"] = json.loads(r["grok_summary"])
            except (TypeError, ValueError):
                pass
        # Legacy schema carried a ``notes`` list; default to empty so /digest's
        # expectations hold.
        entry.setdefault("notes", [])
        papers[r["paper_key"]] = entry
    return {"papers": papers}


# ---------------------------------------------------------------- write APIs (high-level)


def save_config(cfg: dict[str, Any]) -> None:
    """Persist the legacy config dict.

    ``categories`` lands in ``config.json`` (only on actual change — the file
    is only rewritten if categories differ from what's on disk, so daily runs
    that don't touch categories produce no commit-worthy diff).
    ``last_update_id`` and ``sent_ids`` go to D1.
    """
    new_categories = list(cfg.get("categories", []) or DEFAULT_CATEGORIES)
    existing = _load_config_file().get("categories")
    if existing != new_categories:
        _dump_config_file(new_categories)

    set_last_update_id(int(cfg.get("last_update_id", 0) or 0))
    _d1_replace_sent_ids(list(cfg.get("sent_ids", [])))


def save_votes(votes: dict[str, Any]) -> None:
    """Persist the legacy votes dict.

    Per-paper ``liked``/``disliked`` rows are upserted in real time by
    :func:`record_vote`, so this call only refreshes the ``last_batch``
    table (which is wholesale-replaced each daily run).
    """
    replace_last_batch(votes.get("last_batch", {}) or {})


def save_reading_log(log: dict[str, Any]) -> None:
    """No-op kept for main.py call-site compatibility.

    Per-paper changes are upserted as they happen via :func:`upsert_paper_log`
    / :func:`update_paper_status`; nothing else to flush.
    """
    return None


# ---------------------------------------------------------------- narrow mutators


def record_vote(key: str, text: str, bucket: str, ts: str) -> None:
    """Upsert a single vote row. ``bucket`` ∈ {'liked','disliked'}."""
    if bucket not in ("liked", "disliked"):
        raise ValueError(f"bucket must be 'liked' or 'disliked', got {bucket!r}")
    _d1.query(
        "INSERT INTO votes (paper_key, bucket, text, ts) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(paper_key) DO UPDATE SET "
        "  bucket=excluded.bucket, text=excluded.text, ts=excluded.ts",
        [key, bucket, text, ts],
    )
    prune_votes()


def prune_votes(max_per_side: int = MAX_VOTES_PER_SIDE) -> None:
    """Drop oldest votes beyond ``max_per_side`` per bucket (liked / disliked)."""
    for bucket in ("liked", "disliked"):
        _d1.query(
            "DELETE FROM votes WHERE bucket = ? AND paper_key NOT IN ("
            "  SELECT paper_key FROM votes WHERE bucket = ? "
            "  ORDER BY ts DESC LIMIT ?"
            ")",
            [bucket, bucket, int(max_per_side)],
        )


def load_category_preferences() -> dict[str, float]:
    rows = _d1.query("SELECT value FROM kv WHERE key = 'category_preferences'")
    if not rows:
        return {}
    raw = rows[0].get("value") or "{}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items()}


def save_category_preferences(prefs: dict[str, float]) -> None:
    _d1.query(
        "INSERT INTO kv (key, value) VALUES ('category_preferences', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [json.dumps(prefs, sort_keys=True)],
    )


def bump_category_preferences(
    prefs: dict[str, float],
    categories: list[str],
    *,
    delta_primary: float,
    delta_secondary: float = 0.0,
) -> dict[str, float]:
    """Update preference weights in place and return the dict."""
    if not categories:
        return prefs
    primary, *rest = categories
    prefs[primary] = prefs.get(primary, 0.0) + delta_primary
    for cat in rest:
        prefs[cat] = prefs.get(cat, 0.0) + (delta_secondary if delta_secondary else delta_primary * 0.25)
    return prefs


def upsert_paper_log(
    key: str,
    *,
    title: str | None = None,
    url: str | None = None,
    text: str | None = None,
    categories: list[str] | None = None,
    score: float | None = None,
    status: str | None = None,
    status_ts: str | None = None,
    sent_ts: str | None = None,
    created_ts: str | None = None,
    grok_summary: dict[str, Any] | None = None,
) -> None:
    """Insert-or-update a reading_log row. Only non-None fields are written."""
    payload = {
        "title": title,
        "url": url,
        "text": text,
        "categories": json.dumps(categories) if categories is not None else None,
        "score": score,
        "status": status,
        "status_ts": status_ts,
        "sent_ts": sent_ts,
        "grok_summary": json.dumps(grok_summary) if grok_summary is not None else None,
    }
    columns = ["paper_key", "created_ts"] + list(payload.keys())
    values: list[Any] = [key, created_ts or status_ts or sent_ts or ""]
    values.extend(payload.values())

    placeholders = ", ".join("?" for _ in columns)
    update_clauses = [
        f"{col}=COALESCE(excluded.{col}, reading_log.{col})"
        for col in payload.keys()
    ]
    sql = (
        f"INSERT INTO reading_log ({', '.join(columns)}) VALUES ({placeholders}) "
        f"ON CONFLICT(paper_key) DO UPDATE SET {', '.join(update_clauses)}"
    )
    _d1.query(sql, values)


def replace_last_batch(batch: dict[str, dict[str, Any]]) -> None:
    """Atomically replace the ``last_batch`` table contents."""
    statements: list[tuple[str, list[Any] | None]] = [("DELETE FROM last_batch", None)]
    for pos, entry in batch.items():
        if not isinstance(entry, dict):
            continue
        statements.append(
            (
                "INSERT INTO last_batch (position, paper_key, text, score) VALUES (?, ?, ?, ?)",
                [int(pos), entry.get("key"), entry.get("text"), entry.get("score")],
            )
        )
    _d1.batch(statements)


def append_sent_ids(keys: list[str], sent_ts: str) -> None:
    """Insert new ``sent_ids`` rows and trim to ``MAX_SENT_IDS`` newest."""
    if not keys:
        return
    statements: list[tuple[str, list[Any] | None]] = []
    for key in keys:
        statements.append(
            (
                "INSERT INTO sent_ids (paper_key, sent_ts) VALUES (?, ?) "
                "ON CONFLICT(paper_key) DO UPDATE SET sent_ts=excluded.sent_ts",
                [key, sent_ts],
            )
        )
    statements.append(
        (
            "DELETE FROM sent_ids WHERE paper_key NOT IN ("
            "  SELECT paper_key FROM sent_ids ORDER BY sent_ts DESC LIMIT ?"
            ")",
            [MAX_SENT_IDS],
        )
    )
    _d1.batch(statements)


def set_last_update_id(n: int) -> None:
    _d1.query(
        "INSERT INTO kv (key, value) VALUES ('last_update_id', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [str(int(n))],
    )


def set_categories(categories: list[str]) -> None:
    """Persist categories to config.json. The only file write the app ever does."""
    _dump_config_file(list(categories))


# ---------------------------------------------------------------- D1 read helpers


def _d1_get_last_update_id() -> int:
    rows = _d1.query("SELECT value FROM kv WHERE key='last_update_id'")
    if not rows:
        return 0
    try:
        return int(rows[0]["value"])
    except (KeyError, TypeError, ValueError):
        return 0


def _d1_get_sent_ids() -> list[str]:
    rows = _d1.query("SELECT paper_key FROM sent_ids ORDER BY sent_ts ASC")
    return [r["paper_key"] for r in rows if r.get("paper_key")]


def _d1_replace_sent_ids(keys: list[str]) -> None:
    """Wholesale-replace ``sent_ids``. The caller is expected to have already
    capped the list to ``MAX_SENT_IDS``."""
    statements: list[tuple[str, list[Any] | None]] = [("DELETE FROM sent_ids", None)]
    # Preserve in-list order via a synthetic sent_ts that sorts ASC.
    # Real sent_ts values (2020+) sort strictly above these 1970 bootstrap values.
    for idx, key in enumerate(keys):
        ts = f"1970-01-01T00:00:00.{idx:06d}Z"
        statements.append(
            (
                "INSERT INTO sent_ids (paper_key, sent_ts) VALUES (?, ?) "
                "ON CONFLICT(paper_key) DO UPDATE SET sent_ts=excluded.sent_ts",
                [key, ts],
            )
        )
    _d1.batch(statements)
