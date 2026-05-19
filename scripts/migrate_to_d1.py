#!/usr/bin/env python3
"""One-shot backfill of LitFeed JSON state into Cloudflare D1.

Reads ``config.json``, ``votes.json`` and ``reading_log.json`` from the
repo root, and writes their contents into the D1 ``votes``, ``reading_log``,
``sent_ids``, ``last_batch`` and ``kv`` tables.

Idempotent: every INSERT uses ``ON CONFLICT(paper_key) DO UPDATE`` (or
``DO NOTHING`` where appropriate), so re-running the script keeps D1 in
sync with the current JSON without creating duplicates. Safe to run during
the parallel-write soak whenever you want to top up D1 from the canonical
JSON files.

Usage::

    CF_ACCOUNT_ID=...  CF_D1_DATABASE_ID=...  CF_D1_API_TOKEN=...  \
        python scripts/migrate_to_d1.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import _d1  # noqa: E402  (path-injection above)

CONFIG_PATH = REPO_ROOT / "config.json"
VOTES_PATH = REPO_ROOT / "votes.json"
READING_LOG_PATH = REPO_ROOT / "reading_log.json"


def _load(path: Path, default):
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def _migrate_votes(votes: dict, dry_run: bool, verbose: bool) -> tuple[int, int]:
    """Returns (liked_count, disliked_count) inserted."""
    liked = votes.get("liked", []) or []
    disliked = votes.get("disliked", []) or []

    statements = []
    for entry in liked:
        statements.append(
            (
                "INSERT INTO votes (paper_key, bucket, text, ts) VALUES (?, 'liked', ?, ?) "
                "ON CONFLICT(paper_key) DO UPDATE SET bucket='liked', text=excluded.text, ts=excluded.ts",
                [entry["key"], entry.get("text", ""), entry.get("ts", "")],
            )
        )
    for entry in disliked:
        statements.append(
            (
                "INSERT INTO votes (paper_key, bucket, text, ts) VALUES (?, 'disliked', ?, ?) "
                "ON CONFLICT(paper_key) DO UPDATE SET bucket='disliked', text=excluded.text, ts=excluded.ts",
                [entry["key"], entry.get("text", ""), entry.get("ts", "")],
            )
        )

    if verbose:
        print(f"  votes: {len(liked)} liked, {len(disliked)} disliked")
    if not dry_run and statements:
        _d1.batch(statements)
    return len(liked), len(disliked)


def _migrate_last_batch(votes: dict, dry_run: bool, verbose: bool) -> int:
    last_batch = votes.get("last_batch", {}) or {}
    statements = [("DELETE FROM last_batch", None)]
    count = 0
    for pos, entry in last_batch.items():
        if not isinstance(entry, dict):
            continue
        statements.append(
            (
                "INSERT INTO last_batch (position, paper_key, text, score) VALUES (?, ?, ?, ?)",
                [int(pos), entry.get("key"), entry.get("text"), entry.get("score")],
            )
        )
        count += 1
    if verbose:
        print(f"  last_batch: {count} entries")
    if not dry_run and len(statements) > 1:
        _d1.batch(statements)
    elif not dry_run:
        _d1.query("DELETE FROM last_batch")
    return count


def _migrate_reading_log(log: dict, dry_run: bool, verbose: bool) -> int:
    papers = log.get("papers", {}) or {}
    statements = []
    for key, entry in papers.items():
        categories = entry.get("categories")
        grok = entry.get("grok_summary")
        statements.append(
            (
                "INSERT INTO reading_log "
                "(paper_key, title, url, text, categories, score, status, status_ts, sent_ts, created_ts, grok_summary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(paper_key) DO UPDATE SET "
                "  title=COALESCE(excluded.title, reading_log.title), "
                "  url=COALESCE(excluded.url, reading_log.url), "
                "  text=COALESCE(excluded.text, reading_log.text), "
                "  categories=COALESCE(excluded.categories, reading_log.categories), "
                "  score=COALESCE(excluded.score, reading_log.score), "
                "  status=COALESCE(excluded.status, reading_log.status), "
                "  status_ts=COALESCE(excluded.status_ts, reading_log.status_ts), "
                "  sent_ts=COALESCE(excluded.sent_ts, reading_log.sent_ts), "
                "  grok_summary=COALESCE(excluded.grok_summary, reading_log.grok_summary)",
                [
                    key,
                    entry.get("title"),
                    entry.get("url"),
                    entry.get("text"),
                    json.dumps(categories) if categories else None,
                    entry.get("score"),
                    entry.get("status"),
                    entry.get("status_ts"),
                    entry.get("sent_ts"),
                    entry.get("created_ts") or entry.get("sent_ts") or entry.get("status_ts") or "",
                    json.dumps(grok) if grok else None,
                ],
            )
        )
    if verbose:
        print(f"  reading_log: {len(papers)} papers")
    if not dry_run and statements:
        # Chunk to keep batches reasonable (D1 has a limit on statements per call).
        chunk = 50
        for i in range(0, len(statements), chunk):
            _d1.batch(statements[i : i + chunk])
    return len(papers)


def _migrate_sent_ids(cfg: dict, dry_run: bool, verbose: bool) -> int:
    sent_ids = cfg.get("sent_ids", []) or []
    statements: list[tuple[str, list | None]] = [("DELETE FROM sent_ids", None)]
    for idx, key in enumerate(sent_ids):
        # Synthetic ts that preserves the in-file order and sorts strictly
        # below any real (post-2020) sent_ts written from this point on.
        ts = f"1970-01-01T00:00:00.{idx:06d}Z"
        statements.append(
            (
                "INSERT INTO sent_ids (paper_key, sent_ts) VALUES (?, ?) "
                "ON CONFLICT(paper_key) DO UPDATE SET sent_ts=excluded.sent_ts",
                [key, ts],
            )
        )
    if verbose:
        print(f"  sent_ids: {len(sent_ids)} entries")
    if not dry_run and len(statements) > 1:
        chunk = 100
        for i in range(0, len(statements), chunk):
            _d1.batch(statements[i : i + chunk])
    elif not dry_run:
        _d1.query("DELETE FROM sent_ids")
    return len(sent_ids)


def _migrate_kv(cfg: dict, dry_run: bool, verbose: bool) -> None:
    last_update_id = int(cfg.get("last_update_id", 0) or 0)
    if verbose:
        print(f"  kv.last_update_id = {last_update_id}")
    if not dry_run:
        _d1.query(
            "INSERT INTO kv (key, value) VALUES ('last_update_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [str(last_update_id)],
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Plan-only; do not write to D1.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not _d1.is_configured():
        print(
            "D1 not configured. Set CF_ACCOUNT_ID, CF_D1_DATABASE_ID, CF_D1_API_TOKEN.",
            file=sys.stderr,
        )
        return 2

    if not args.dry_run and not _d1.health_check():
        print("D1 health check failed; refusing to migrate.", file=sys.stderr)
        return 3

    cfg = _load(CONFIG_PATH, {})
    votes = _load(VOTES_PATH, {})
    log = _load(READING_LOG_PATH, {})

    print("Backfilling D1 from JSON state files…")
    liked, disliked = _migrate_votes(votes, args.dry_run, args.verbose)
    last_batch_count = _migrate_last_batch(votes, args.dry_run, args.verbose)
    reading_count = _migrate_reading_log(log, args.dry_run, args.verbose)
    sent_count = _migrate_sent_ids(cfg, args.dry_run, args.verbose)
    _migrate_kv(cfg, args.dry_run, args.verbose)

    suffix = " (dry run)" if args.dry_run else ""
    print(
        f"Done{suffix}. votes={liked + disliked}, last_batch={last_batch_count}, "
        f"reading_log={reading_count}, sent_ids={sent_count}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
