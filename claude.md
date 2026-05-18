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

`/list`, `/add_cat <arxiv.cat>`, `/rm_cat <arxiv.cat>`, `/reset`,
`/like N [N …]`, `/dislike N [N …]`, `/why N`, `/stats`, `/help`. Non-owner
senders are ignored.

Two ways to vote. Per-paper `v:like:`/`v:dislike:` inline buttons on each run
message (`handle_callback`). Or batch: each run numbers papers (`[1]`, `[2]`,
…) and stores the number→paper map in `votes["last_batch"]`; `/like` /
`/dislike` (`handle_vote_command`) resolve numbers against it. `/why N`
explains why paper `N` matched your profile.

## State Files (committed back to the repo by CI)

* `config.json` — `categories`, `last_update_id` (Telegram offset), `sent_ids`
  (dedup ring buffer, capped at `MAX_SENT_IDS`).
* `votes.json` — `liked`, `disliked` (each `{key, text, ts}`); `last_batch`
  (batch number → `{key, text}`) for `/like` / `/dislike` and callback votes.
  This keeps runtime state compact and avoids long-lived sent caches.

`DEFAULT_CATEGORIES` in `main.py` is the seed list (CS / math / physics /
q-fin / stats — reflecting interests in computational physics, plasma/fusion,
and quantitative finance). The live set is whatever is in `config.json`.

## GitHub Actions

* `daily_papers.yml` — full run. Triggered by `repository_dispatch`
  (`run-paper-alerter`, fired by cron-job.org) and `workflow_dispatch`.
  Maps `TELEGRAM_TOKEN` / `CHAT_ID` secrets to env vars.
* `poll_commands.yml` — every 5 minutes, `python main.py --commands-only`, so
  commands/votes are handled promptly without waiting for the daily run.

Both commit `config.json` / `votes.json` changes back with rebase. They share a
`telegram-poll` concurrency group so two runs never push stale state. `sync.sh`
is a local helper to rebase-and-push around those bot commits.

## Conventions

* Secrets (`TELEGRAM_TOKEN`, `CHAT_ID`) come from env vars only — never
  hardcoded.
* Telegram and arXiv HTTP failures are caught; arXiv 429s get exponential
  backoff (`ARXIV_MAX_ATTEMPTS`, `ARXIV_BACKOFF_BASE`) and a descriptive
  `User-Agent`.
* No new papers on a given day → exit quietly, no blank message.
* `arxiv:` short IDs are stored version-stripped so `v1`/`v2` don't both alert.
