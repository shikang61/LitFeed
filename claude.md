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
* **Automation:** GitHub Actions (two workflows)
* **Delivery:** Telegram Bot API

## How It Works

A run (`python main.py`) does two things in order:

1. **Process Telegram updates.** Polls `getUpdates` for owner commands and
   inline-keyboard vote callbacks, mutating `config.json` / `votes.json`.
2. **Fetch, filter, alert** (skipped under `--commands-only`):
   * Fetch papers from each category's RSS feed at
     `https://rss.arxiv.org/rss/<category>`. RSS is a separate cached host with
     looser rate limits than the legacy `export.arxiv.org` API, which now
     returns HTTP 429 for GitHub Actions runner IPs.
   * Keep only `announce_type` `new`/`cross`; dedup cross-listed papers; drop
     anything older than `LOOKBACK_HOURS` (36) as a defensive floor.
   * Drop papers already in `config.json`'s `sent_ids`.
  * **Filter:** if there are ≥ `MIN_VOTES_PER_SIDE` (10) likes *and* dislikes,
    score each paper with the TF-IDF recommender (recency-weighted votes), rank
    by relevance then freshness, apply a diversity guardrail, and keep papers
    above a dynamic relevance floor. Otherwise (cold start), send freshest
    papers with the same diversity guardrail.
   * Cap to `MAX_PAPERS_PER_RUN` (5) total papers per run. Category capping can
     be disabled so the best papers win globally.
   * Send each survivor to Telegram (Markdown, 👍/👎 buttons), record it in
     `sent_ids` and `votes["last_batch"]`.

### Recommender (`recommender.py`)

`fit(liked_docs, disliked_docs)` builds a TF-IDF model (1–2 grams, English
stop words) and liked/disliked centroids. `score(text, model)` returns
`cos(text, liked_centroid) − cos(text, disliked_centroid)`. sklearn is imported
lazily so `--commands-only` runs don't need it.

## Telegram Commands (owner only)

`/list`, `/reset`, `/digest`, `/why N`, `/stats`, `/help`. Non-owner senders
are ignored.

Voting and triage are button-only. Each paper alert carries an inline keyboard
with 👍 Like / 👎 Dislike (`v:like:<key>` / `v:dislike:<key>` callbacks),
Read (forwards to the To Read group, `h:read_to_group:<key>`), and Delete
(`h:delete:<key>` → confirm/cancel). The Cloudflare Worker answers each
callback instantly with a toast and dispatches a `repository_dispatch` event
so `main.py --apply-update` can mutate `votes.json` / `reading_log.json`
asynchronously. `/why N` still explains why paper `N` matched your profile.

## State Files (committed back to the repo by CI)

* `config.json` — `categories`, `last_update_id` (Telegram offset), `sent_ids`
  (dedup ring buffer, capped at `MAX_SENT_IDS`).
* `votes.json` — `liked`, `disliked` (each `{key, text, ts}`); `last_batch`
  (batch number → `{key, text}`) used by callback votes and `/why N`.
  This keeps runtime state compact and avoids long-lived sent caches.

`DEFAULT_CATEGORIES` in `main.py` is the seed list (CS / math / physics /
q-fin / stats — reflecting interests in computational physics, plasma/fusion,
and quantitative finance). The live set is whatever is in `config.json`.

## GitHub Actions

* `daily_papers.yml` — full run. Triggered by `repository_dispatch`
 (`run-paper-alerter`, fired by cron-job.org) and `workflow_dispatch`.
 Maps `TELEGRAM_TOKEN` / `CHAT_ID` secrets to env vars. Sets
 `LITFEED_DISABLE_POLL=1` so it skips `getUpdates` (the Cloudflare Worker
 webhook is the consumer).
* `process_update.yml` — triggered by `repository_dispatch` event type
 `telegram-update`, fired by the Cloudflare Worker (`worker/index.js`) for any
 update that needs server-side state mutation. Runs `python main.py
 --apply-update`, reading the update payload from `LITFEED_UPDATE_JSON` and
 the `LITFEED_WEBHOOK_HANDLED` flag from the dispatch `client_payload`.
* `poll_commands.yml` — legacy fallback. Cron is commented out; only
 `workflow_dispatch` remains. Re-enable the cron and remove
 `LITFEED_DISABLE_POLL` if you delete the webhook.
* `weekly_digest.yml` — Sunday 18:00 UTC, `python main.py --weekly-digest`.

All workflows commit `config.json` / `votes.json` / `reading_log.json` changes
back with rebase. They share a `telegram-poll` concurrency group so two runs
never push stale state. `sync.sh` is a local helper to rebase-and-push around
those bot commits.

## Cloudflare Worker (optional, for instant button reactions)

`worker/index.js` is the Telegram webhook receiver. It handles
`h:read_to_group` / `h:delete` / `h:confirm_delete` / `h:cancel_delete`
callbacks directly via the Bot API in <1s, and forwards everything else
(commands, votes, legacy callbacks) to GitHub via `repository_dispatch`. For
`h:read_to_group` it does both: forward instantly, then dispatch with
`webhook_handled=true` so `reading_log.json` is updated to `saved`.

Telegram only allows one update consumer at a time. While a webhook is set,
`getUpdates` returns HTTP 409, so `poll_commands.yml`'s cron stays disabled.
Setup is documented in `worker/README.md`.

## Conventions

* Secrets (`TELEGRAM_TOKEN`, `CHAT_ID`) come from env vars only — never
  hardcoded.
* Telegram and arXiv HTTP failures are caught; arXiv 429s get exponential
  backoff (`ARXIV_MAX_ATTEMPTS`, `ARXIV_BACKOFF_BASE`) and a descriptive
  `User-Agent`.
* No new papers on a given day → exit quietly, no blank message.
* `arxiv:` short IDs are stored version-stripped so `v1`/`v2` don't both alert.
