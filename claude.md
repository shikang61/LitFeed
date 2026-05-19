# Project Overview

LitFeed is an autonomous intra-day arXiv → Telegram alerter. Each run fetches newly
announced papers in the configured arXiv categories, ranks them with a
preference filter learned from the user's 👍/👎 votes, and pushes the survivors
to Telegram with vote buttons. It runs unattended on GitHub Actions.

## Tech Stack

* **Language:** Python 3.11 (pinned in the workflows)
* **Libraries:** `feedparser` (arXiv RSS), `requests` (arXiv + Telegram HTTP),
 `scikit-learn` + `numpy` (TF-IDF recommender), `pylatexenc` (LaTeX → Unicode
 in titles/abstracts)
* **Automation:** GitHub Actions (daily, weekly, on-demand webhook)
* **State:** Cloudflare D1 (SQLite at the edge) for all runtime state;
 `config.json` in git for the static `categories` list. The D1 cutover
 history lives in `docs/d1_migration.md`; the day-to-day abstraction
 layer is `state_store.py`.
* **Delivery:** Telegram Bot API via a Cloudflare Worker webhook

## How It Works

A run (`python main.py`) does two things in order:

1. **Process Telegram updates** (legacy `getUpdates` poll; in production this
   is skipped via `LITFEED_DISABLE_POLL=1` because the Cloudflare Worker
   is the webhook consumer). `main.py --apply-update` handles one update
   delivered by the Worker via `repository_dispatch`.
2. **Fetch, filter, alert** (skipped under `--commands-only`):
   * Fetch papers from each category's RSS feed at
     `https://rss.arxiv.org/rss/<category>`. RSS is a separate cached host with
     looser rate limits than the legacy `export.arxiv.org` API, which now
     returns HTTP 429 for GitHub Actions runner IPs.
   * Keep only `announce_type` `new`/`cross`; dedup cross-listed papers; drop
     anything older than `LOOKBACK_HOURS` (36) as a defensive floor.
   * Drop papers already in D1's `sent_ids` ring buffer.
  * **Filter:** if there are ≥ `MIN_VOTES_PER_SIDE` (10) likes *and* dislikes,
    score each paper with the TF-IDF recommender (recency-weighted votes), rank
    by relevance then freshness, apply a diversity guardrail, and keep papers
    above a dynamic relevance floor. Otherwise (cold start), send freshest
    papers with the same diversity guardrail.
   * Cap to `MAX_PAPERS_PER_RUN` (5) total papers per run. Category capping can
     be disabled so the best papers win globally.
   * Send each survivor to Telegram (Markdown, 👍/👎 buttons), record it in
     D1 (`sent_ids` + `reading_log` + `last_batch`).

### Recommender (`recommender.py` + selection helpers in `main.py`)

`fit(liked_docs, disliked_docs)` builds a TF-IDF model (1–2 grams, English
stop words, sublinear TF) and recency-weighted liked/disliked centroids
(45-day half-life). `score(text, model)` returns
`cos(text, liked_centroid) − cos(text, disliked_centroid)`. sklearn is
imported lazily so `--commands-only` runs don't need it.

The full algorithm (cold-start gate, relevance floor, diversity guardrail,
serendipity slot, priority mix, weekly-digest deep-read pick, tunable
knobs, and ideas for improvement) is documented end-to-end in
[docs/recommender.md](docs/recommender.md). Revisit that doc before
changing scoring behavior.

## Telegram Commands (owner only)

`/reset`, `/digest`, `/stats`, `/help`. Non-owner senders
are ignored.

Voting and triage are button-only. Each paper alert carries an inline keyboard
with 👍 Like / 👎 Dislike (`v:like:<key>` / `v:dislike:<key>` callbacks),
Read (forwards to the To Read group, `h:read_to_group:<key>`), and Delete
(`h:delete:<key>` → confirm/cancel). The Cloudflare Worker answers each
callback instantly with a toast and writes vote / read-saved upserts to D1
directly. Only `/commands` (which build multi-line Markdown replies) are
forwarded to GitHub via `repository_dispatch` so `main.py --apply-update`
can compose them. Each paper alert carries the recommender's relevance
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
* `kv(key PK, value)` — small scalars (currently just `last_update_id`).

`config.json` keeps **only** the user-edited `categories` list, version-
controlled in git. `DEFAULT_CATEGORIES` in `main.py` is the seed list
(CS / math / physics / q-fin / stats — reflecting interests in
computational physics, plasma/fusion, and quantitative finance). The live
set is whatever is in `config.json`.

### Local dev

D1 is the only backend. Export the three `CF_*` env vars in your shell
(the same ones the workflows use) and `python main.py` will read/write D1
exactly like CI does. There's no JSON fallback — the legacy
`LITFEED_USE_LOCAL_JSON` / `LITFEED_DISABLE_JSON_WRITE` / `LITFEED_READ_FROM`
escape hatches were retired in Phase F.

## GitHub Actions

* `daily_papers.yml` — full run. Triggered by `repository_dispatch`
 (`run-paper-alerter`, fired by cron-job.org) and `workflow_dispatch`.
 Maps `TELEGRAM_TOKEN` / `CHAT_ID` secrets to env vars. Sets
 `LITFEED_DISABLE_POLL=1` so it skips `getUpdates` (the Cloudflare Worker
 webhook is the consumer).
* `process_update.yml` — triggered by `repository_dispatch` event type
 `telegram-update`, fired by the Cloudflare Worker (`worker/index.js`) for
 updates that need the full Python environment (today: `/commands` only;
 the Worker writes votes/reads to D1 directly). Runs `python main.py
 --apply-update`, reading the update payload from `LITFEED_UPDATE_JSON` and
 the `LITFEED_WEBHOOK_HANDLED` flag from the dispatch `client_payload`.
* `poll_commands.yml` — legacy fallback. Cron is commented out; only
 `workflow_dispatch` remains. Re-enable the cron and remove
 `LITFEED_DISABLE_POLL` if you delete the webhook.
* `weekly_digest.yml` — Sunday 18:00 UTC, `python main.py --weekly-digest`.

`daily_papers.yml` and `weekly_digest.yml` are pure read+D1-write
workflows now — they don't commit anything back to the repo.
`process_update.yml` still uses `scripts/safe_state_push.sh` to commit
`config.json` whenever a command (today, only `/reset`) rewrites
categories. All three share a `telegram-poll` concurrency group so they
never push concurrently. `sync.sh` is a local helper to rebase-and-push
around those bot commits.

## Cloudflare Worker (for instant button reactions)

`worker/index.js` is the Telegram webhook receiver. It splits work two ways:

* **Direct D1 writes (no GitHub):** `v:like` / `v:dislike` upsert into the
 `votes` table; `h:read_to_group` upserts `reading_log.status='saved'`.
 The Worker has a D1 binding (`env.DB`, declared in `worker/wrangler.toml`)
 and runs the upserts in single-digit ms.
* **GitHub dispatch:** anything else — i.e. `/commands` like
 `/reset`, `/digest`, `/stats`, `/help` — fires
 `repository_dispatch` so `process_update.yml` runs
 `python main.py --apply-update`. Commands need the full Python env to
 build multi-line Markdown messages or call the recommender.

`h:delete` / `h:confirm_delete` / `h:cancel_delete` are Telegram-only
(keyboard swap, deleteMessage) and never touch D1 or GitHub.

Telegram only allows one update consumer at a time. While a webhook is set,
`getUpdates` returns HTTP 409, so `poll_commands.yml`'s cron stays disabled.
Setup is documented in `worker/README.md`; the D1 cutover history (now
complete) is in `docs/d1_migration.md`.

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
  / `save_reading_log` calls so concurrent Worker writes aren't clobbered.
