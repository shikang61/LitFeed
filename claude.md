# Project Overview

LitFeed is a daily arXiv → Telegram alerter with a preference filter learned
from the user's 👍/👎 votes. A GitHub Action does the daily fetch-and-alert; a
Cloud Run webhook handles Telegram votes and `/commands` in real time. All
mutable state lives in Firestore.

## Tech Stack

* **Language:** Python 3.11
* **Libraries:** `feedparser` (arXiv RSS), `requests` (arXiv + Telegram HTTP),
  `scikit-learn` + `numpy` (TF-IDF recommender), `pylatexenc` (LaTeX → Unicode),
  `google-cloud-firestore` (state), `flask` + `gunicorn` (webhook server)
* **Hosting:** GitHub Actions (daily job) + Cloud Run (webhook)
* **State:** Firestore
* **Delivery:** Telegram Bot API

## Architecture

```
GitHub Action (daily)  ──fetch+filter+alert──>  Telegram
        │                                          │
        └────────────> Firestore <─────────────────┘
                          ▲          button press / command
                   Cloud Run webhook <─────────────┘
```

Telegram delivers updates via **webhook** (no polling — webhook and
`getUpdates` are mutually exclusive). So both votes *and* `/commands` flow
through Cloud Run; the daily Action only fetches and alerts.

## Components

* **`main.py`** — daily job (`python main.py`): read config + votes from
  Firestore, fetch papers, filter, send alerts, write `sent_ids` /
  `sent_cache` back. Also defines `handle_command` / `handle_callback` /
  `send_message`, reused by the webhook.
* **`webhook.py`** — Cloud Run Flask app. `/webhook` POST verifies the
  `X-Telegram-Bot-Api-Secret-Token` header, checks owner id, dispatches to
  `handle_callback` / `handle_command`. `/` is a health check.
* **`store.py`** — Firestore data layer, shared by `main.py` and `webhook.py`.
* **`recommender.py`** — TF-IDF model. `fit(liked, disliked)` builds 1–2 gram
  TF-IDF + liked/disliked centroids; `score(text, model)` returns
  `cos(text, liked) − cos(text, disliked)`. sklearn imported lazily.
* **`migrate_to_firestore.py`** — one-time `config.json`/`votes.json` → Firestore.

## How the daily run works

1. `fetch_recent_papers` pulls each category's RSS feed at
   `https://rss.arxiv.org/rss/<category>` — a separate cached host with looser
   limits than the legacy `export.arxiv.org` API (which 429s GitHub runner IPs).
2. Keep `announce_type` `new`/`cross`; dedup cross-listed papers; drop anything
   older than `LOOKBACK_HOURS` (36) and anything already in `sent_ids`.
3. **Filter:** with ≥ `MIN_VOTES_PER_SIDE` (10) likes *and* dislikes, score each
   paper with the recommender and keep score > 0; otherwise (cold start) send all.
4. Cap to `PER_CATEGORY_LIMIT` (2) per primary category.
5. Send survivors to Telegram with 👍/👎 buttons; record in `sent_ids` +
   `sent_cache`.

## Firestore schema

* `state/config` (doc) — `{ categories, sent_ids }`
* `votes/{paper_key}` (docs) — `{ bucket: "liked"|"disliked", text, ts }`.
  Doc id = paper key, so re-voting overwrites in place.
* `sent_cache/{paper_key}` (docs) — `{ text, message_id, ts }`. Pruned to
  `MAX_SENT_CACHE`; a vote on a paper no longer cached is rejected with a toast.

`MAX_SENT_IDS` / `MAX_SENT_CACHE` (both 500) live in `store.py`.

## Telegram commands (owner only)

`/list`, `/add_cat <arxiv.cat>`, `/rm_cat <arxiv.cat>`, `/reset`, `/stats`,
`/help`. Non-owner senders ignored. `DEFAULT_CATEGORIES` in `main.py` is the
seed list (CS / math / physics / q-fin / stats — computational physics,
plasma/fusion, quantitative finance).

## Deployment

* **GitHub Action** (`.github/workflows/daily_papers.yml`) — `repository_dispatch`
  (`run-paper-alerter`, fired by cron-job.org), fallback `schedule` cron,
  `workflow_dispatch`. Authenticates to GCP via `google-github-actions/auth`
  using Workload Identity Federation (keyless — org policy blocks SA keys):
  secrets `WIF_PROVIDER` + `WIF_SERVICE_ACCOUNT`, and `permissions: id-token:
  write`. `TELEGRAM_TOKEN` / `CHAT_ID` are also secrets.
* **Cloud Run** — built from `Dockerfile` (`gunicorn webhook:app`). Env vars:
  `TELEGRAM_TOKEN`, `CHAT_ID`, `WEBHOOK_SECRET`. Uses its service-account
  identity for Firestore. Register with Telegram via `setWebhook` once.

## Conventions

* Secrets (`TELEGRAM_TOKEN`, `CHAT_ID`, `WEBHOOK_SECRET`, `WIF_PROVIDER`,
  `WIF_SERVICE_ACCOUNT`) come from env / GitHub secrets only — never hardcoded.
* Firestore auth is keyless: `firestore.Client()` auto-picks the credentials
  that `google-github-actions/auth` exports (the Action, via WIF) or the Cloud
  Run service account.
* Telegram and arXiv HTTP failures are caught; arXiv 429s get exponential
  backoff (`ARXIV_MAX_ATTEMPTS`, `ARXIV_BACKOFF_BASE`) and a descriptive
  `User-Agent`.
* No new papers on a given day → exit quietly, no blank message.
* `arxiv:` short IDs are stored version-stripped so `v1`/`v2` don't both alert.
