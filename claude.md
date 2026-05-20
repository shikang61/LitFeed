# Project Overview

LitFeed is an autonomous intra-day arXiv → Telegram alerter. Each run fetches newly
announced papers in the configured arXiv categories, ranks them with a
preference filter learned from the user's 👍/👎 votes, and pushes the survivors
to Telegram with vote buttons. It runs unattended on GitHub Actions.

## Tech Stack

* **Language:** Python 3.11 (pinned in the workflows)
* **Libraries:** `feedparser` (arXiv RSS), `requests` (arXiv + Telegram HTTP),
 `scikit-learn` + `numpy` (TF-IDF recommender),
 `sentence-transformers` (`all-MiniLM-L6-v2` embedding branch, blended with
 TF-IDF), `pylatexenc` (LaTeX → Unicode in titles/abstracts)
* **Automation:** GitHub Actions (daily, weekly, on-demand webhook)
* **State:** Cloudflare D1 (SQLite at the edge) for all runtime state;
 `shared/default_categories.json` for the seed category list; optional
 `config.json` for live overrides (Worker-written on `/reset`). The D1 cutover
 history lives in `docs/d1_migration.md`; the day-to-day abstraction
 layer is `state_store.py`.
* **Delivery:** Telegram Bot API via a Cloudflare Worker webhook

## How It Works

A daily run (`python main.py`) fetches, filters, and alerts. All Telegram
commands and button callbacks are handled by the Cloudflare Worker
(`worker/index.js`). GitHub Actions only runs `python main.py` (daily) and
`python main.py --weekly-digest` (Sunday cron).

**Fetch, filter, alert** (skipped under `--weekly-digest`):
   * Fetch papers from each category's RSS feed at
     `https://rss.arxiv.org/rss/<category>`. RSS is a separate cached host with
     looser rate limits than the legacy `export.arxiv.org` API, which now
     returns HTTP 429 for GitHub Actions runner IPs.
   * Keep only `announce_type` `new`/`cross`; dedup cross-listed papers; drop
     anything older than `LOOKBACK_HOURS` (36) as a defensive floor.
   * Drop papers already in D1's `sent_ids` ring buffer.
  * **Filter:** if there are ≥ `MIN_VOTES_PER_SIDE` (10) likes *and* dislikes,
    score each paper with the recommender (recency-weighted votes), rank
    by relevance then freshness, apply a diversity guardrail, and keep papers
    above a dynamic relevance floor. Otherwise (cold start), send freshest
    papers with the same diversity guardrail.
   * Cap to `MAX_PAPERS_PER_RUN` (10) total papers per run. Category capping can
     be disabled so the best papers win globally.
   * Send each survivor to Telegram (Markdown, 👍/👎 buttons), record it in
     D1 (`sent_ids` + `reading_log` + `last_batch`).

### Recommender (`recommender.py` + selection helpers in `main.py`)

The score blends two branches (`DEFAULT_TFIDF_BLEND = 0.5`):

* **TF-IDF:** `fit(liked_docs, disliked_docs)` builds a TF-IDF model (1–2
  grams, English stop words, sublinear TF) and recency-weighted liked/disliked
  centroids (45-day half-life). `score(text, model)` returns
  `cos(text, liked_centroid) − cos(text, disliked_centroid)`.
* **Embedding:** encode liked/disliked docs with `sentence-transformers`
  (`all-MiniLM-L6-v2`), build recency-weighted centroids, score by the same
  cosine difference. The encoder is memoized via `_get_encoder()`
  (`@lru_cache`) so weights load once per process, not per fit/score call.

sklearn and sentence-transformers are imported lazily, so a minimal import of
`recommender` requires neither. Set `LITFEED_DISABLE_EMBEDDINGS=1` to skip the
embedding branch (TF-IDF only). The model is fetched from the HuggingFace hub;
both workflows cache `~/.cache/huggingface` and run with `HF_HUB_OFFLINE=1` /
`TRANSFORMERS_OFFLINE=1` so daily runs never hit the hub (avoids 429s on the
shared GitHub Actions runner IPs). Cache-miss runs pre-download once.

The full algorithm (cold-start gate, relevance floor, diversity guardrail,
serendipity slot, priority mix, weekly-digest deep-read pick, tunable
knobs, and ideas for improvement) is documented end-to-end in
[docs/recommender.md](docs/recommender.md). Revisit that doc before
changing scoring behavior.

## Telegram Commands (owner only)

`/reset`, `/clear`, `/stats`, `/help`. Non-owner senders are ignored. All of
these (plus votes, Read, Delete) are handled in the Worker. Weekly digest is
scheduled only (`weekly_digest.yml`); there is no `/digest` command.

Voting and triage are button-only. Each paper alert carries an inline keyboard
with 👍 Like / 👎 Dislike (`v:like:<key>` / `v:dislike:<key>` callbacks),
Read (forwards to the To Read group, `h:read_to_group:<key>`), and Delete
(`h:delete:<key>` → confirm/cancel). The Worker answers each callback instantly
and writes vote / read-saved upserts to D1 directly. Each paper alert carries the recommender's relevance
score (or `-` during cold start) directly in the message header.

## State

Runtime state lives in **Cloudflare D1** (database `litfeed_state`), accessed
two ways:

* The Cloudflare Worker (`worker/index.js`) uses the native `env.DB`
 binding for per-row upserts on vote / read-saved callbacks.
* `main.py` (running on GitHub Actions) uses the D1 REST API via
 `_d1.py`. `state_store.py` is the single abstraction layer; `main.py`
 calls `load_config / load_votes / load_reading_log` and narrow mutators
 (`record_vote`, `upsert_paper_log`, `replace_last_batch`, …) and never
 touches the storage backend directly.

D1 tables (see `migrations/0001_init.sql`):

* `votes(paper_key PK, bucket, text, ts)` — current liked/disliked corpus.
* `reading_log(paper_key PK, title, url, text, categories, score, status,
 status_ts, sent_ts, created_ts, grok_summary)` — every paper we've ever
 sent or saved.
* `sent_ids(paper_key PK, sent_ts)` — dedup ring buffer, trimmed to
 `MAX_SENT_IDS` after each daily run.
* `last_batch(position PK, paper_key, text, score)` — wholly replaced each
 daily run; used by Worker vote callbacks to look up text when a paper
 hasn't been written to `reading_log` yet.
* `kv(key PK, value)` — small scalars (`category_preferences`, legacy
  `last_update_id` from the old getUpdates poll).

`shared/default_categories.json` is the **only** seed list (used when
`config.json` is absent). `/reset` and `/clear` write live categories to
`config.json` via the Worker's GitHub Contents API; daily runs prefer that
file when checked out, otherwise fall back to the seed file.

### Local dev

D1 is the only backend. Export the three `CF_*` env vars in your shell
(the same ones the workflows use) and `python main.py` will read/write D1
exactly like CI does. There's no JSON fallback — the legacy
`LITFEED_USE_LOCAL_JSON` / `LITFEED_DISABLE_JSON_WRITE` / `LITFEED_READ_FROM`
escape hatches were retired in Phase F.

## GitHub Actions

* `daily_papers.yml` — full run. Triggered by `repository_dispatch`
 (`run-paper-alerter`, fired by cron-job.org) and `workflow_dispatch`.
 Maps `TELEGRAM_TOKEN` / `CHAT_ID` secrets to env vars.
* `weekly_digest.yml` — Sunday 18:00 UTC, `python main.py --weekly-digest`.

`daily_papers.yml` and `weekly_digest.yml` are pure read+D1-write workflows;
they do not commit back to the repo. Both share a `litfeed-runs` concurrency
group so they never run D1 writes concurrently.

## Cloudflare Worker (for instant button reactions)

`worker/index.js` is the Telegram webhook receiver. It handles all owner
traffic: `v:like` / `v:dislike` and `h:read_to_group` write D1 directly;
`/stats`, `/help`, `/clear`, `/reset`, and `h:confirm_clear` run in the
Worker. `/reset` and confirmed `/clear` also commit `config.json` via the
GitHub Contents API (`GITHUB_REPO` + `GITHUB_PAT`).

`h:delete` / `h:confirm_delete` / `h:cancel_delete` are Telegram-only
(keyboard swap, deleteMessage) and never touch D1.

Telegram only allows one update consumer at a time. Production uses the Worker
webhook exclusively (`getUpdates` polling was removed from `main.py`). Setup is
documented in `worker/README.md`; the D1 cutover history is in
`docs/d1_migration.md`.

## Conventions

* Secrets (`TELEGRAM_TOKEN`, `CHAT_ID`, `CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID`,
  `CF_D1_API_TOKEN`) come from env vars only — never hardcoded.
* Telegram and arXiv HTTP failures are caught; arXiv 429s get exponential
  backoff (`ARXIV_MAX_ATTEMPTS`, `ARXIV_BACKOFF_BASE`) and a descriptive
  `User-Agent`.
* No new papers on a given day → exit quietly, no blank message.
* `arxiv:` short IDs are stored version-stripped so `v1`/`v2` don't both alert.
* State mutations must go through `state_store` (never call `_d1` directly
  from `main.py`). Prefer the narrow mutators (`record_vote`,
  `upsert_paper_log`, `replace_last_batch`, …) over wholesale `save_votes`
  calls so concurrent Worker writes aren't clobbered.
