-- LitFeed D1 schema (initial)
--
-- Applied with:
--   wrangler d1 execute litfeed_state --remote --file migrations/0001_init.sql
--
-- All writes are per-row INSERT ... ON CONFLICT DO UPDATE, so two concurrent
-- runs writing different rows never conflict, and two concurrent runs writing
-- the same row serialize with last-write-wins (which is what we want for votes:
-- a flipped 👍 → 👎 should follow the latest action).

-- One row per (paper) currently in the liked/disliked corpus that feeds the
-- TF-IDF recommender. We don't keep a vote *history* — each paper has one
-- current bucket; re-voting just upserts.
CREATE TABLE IF NOT EXISTS votes (
  paper_key TEXT PRIMARY KEY,
  bucket    TEXT NOT NULL CHECK (bucket IN ('liked','disliked')),
  text      TEXT NOT NULL,
  ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS votes_bucket_ts ON votes(bucket, ts DESC);

-- One row per paper we've ever sent or saved. Replaces reading_log.json's
-- "papers" dict.
CREATE TABLE IF NOT EXISTS reading_log (
  paper_key    TEXT PRIMARY KEY,
  title        TEXT,
  url          TEXT,
  text         TEXT,
  categories   TEXT,                 -- JSON-encoded array of arXiv category codes
  score        REAL,
  status       TEXT,                 -- 'sent' | 'saved' | …
  status_ts    TEXT,
  sent_ts      TEXT,
  created_ts   TEXT NOT NULL,
  grok_summary TEXT                  -- JSON-encoded {text, model, ts}
);
CREATE INDEX IF NOT EXISTS reading_log_status_ts ON reading_log(status_ts DESC);

-- Dedup ring buffer of recently-sent arXiv short IDs (without version suffix).
-- Capped at MAX_SENT_IDS by daily_papers.yml after each run.
CREATE TABLE IF NOT EXISTS sent_ids (
  paper_key TEXT PRIMARY KEY,
  sent_ts   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sent_ids_ts ON sent_ids(sent_ts DESC);

-- The most recent daily batch, numbered 1..N (used by /why N and vote callbacks
-- to look up the abstract text for a paper key). Wholly replaced each daily run.
CREATE TABLE IF NOT EXISTS last_batch (
  position  INTEGER PRIMARY KEY,
  paper_key TEXT NOT NULL,
  text      TEXT,
  score     REAL
);

-- Catch-all key/value table for small scalars. Currently holds:
--   'last_update_id'  → integer-as-string (Telegram offset for getUpdates poll)
CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT
);
