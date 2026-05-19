# LitFeed

Daily arXiv paper alerts delivered to Telegram. Runs autonomously via GitHub Actions, with sub-second button reactions through a Cloudflare Worker webhook.

Checks configured arXiv categories once per day and sends a Markdown-formatted message per paper, each with 👍/👎 and reading-habit buttons. Vote on papers to train a TF-IDF preference filter that progressively narrows what you see, and use the reading log to save papers to your To Read queue.

## Architecture

```
Telegram ── webhook ──► Cloudflare Worker
                              │
                              ├─ instant: 👍/👎, Read, Delete,
                              │           /stats, /help, /clear, /reset
                              │           (D1 + Telegram; config.json via GitHub API)
                              │
GitHub Actions cron ──► daily_papers.yml ── arXiv RSS → filter → Telegram
GitHub Actions cron ──► weekly_digest.yml ── python main.py --weekly-digest
```

The Worker (`worker/`) is the **only** Telegram update consumer. GitHub Actions only runs the batch brain (daily fetch and Sunday digest). Setup: [`worker/README.md`](worker/README.md). Deeper detail: [`docs/architecture.md`](docs/architecture.md).

## Setup

### 1. Create a Telegram bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot` and follow the prompts (name + username).
3. BotFather returns an HTTP API token like `123456:ABC-DEF...`. Save it — this is your `TELEGRAM_TOKEN`.

### 2. Get your chat ID

1. Start a conversation with your new bot (send it any message, e.g. `/start`).
2. In a browser, open:
   ```
   https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```
   (Only works **before** you set a webhook; afterward use the Worker logs or message the bot and inspect D1.)
3. Find the `"chat":{"id": ...}` field in the JSON. That number is your `CHAT_ID`.

### 3. Add GitHub secrets

In the repository: **Settings → Secrets and variables → Actions → New repository secret**.

| Name              | Value                                       |
|-------------------|---------------------------------------------|
| `TELEGRAM_TOKEN`  | The token from BotFather                    |
| `CHAT_ID`         | Your owner chat ID                          |
| `LITFEED_TO_READ_CHAT_ID` | Optional target group chat ID for the `Read` button |
| `GROK_API_KEY`    | Optional xAI API key for Grok summaries     |
| `CF_ACCOUNT_ID`   | Cloudflare account ID (D1)                  |
| `CF_D1_DATABASE_ID` | D1 database ID (`litfeed_state`)        |
| `CF_D1_API_TOKEN` | API token with D1:Edit                      |

Optionally set a repository variable named `GROK_MODEL` to override the default Grok model (`grok-4.3`).

### 4. Enable the workflow

The workflow runs daily at **08:00 UTC**. To test immediately, go to **Actions → Daily arXiv Paper Alerts → Run workflow**.

### 5. Deploy the Cloudflare Worker (required for Telegram)

Setup is documented in [`worker/README.md`](worker/README.md). High-level:

1. `cd worker && wrangler deploy`
2. `wrangler secret put` for `TELEGRAM_TOKEN`, `CHAT_ID`, `LITFEED_TO_READ_CHAT_ID`, `GITHUB_REPO`, `GITHUB_PAT`, `WEBHOOK_SECRET`.
3. Apply D1 migrations: `wrangler d1 execute litfeed_state --remote --file ../migrations/0001_init.sql` (and later migrations as needed).
4. `curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook -d url=<WORKER_URL> -d secret_token=<WEBHOOK_SECRET>`.

Once a webhook is set, Telegram delivers updates only to the Worker (not `getUpdates`).

## Customising via Telegram

Voting and triage use the inline buttons under each paper alert (see *Reading habit loop* below). All owner commands are answered by the Worker.

| Command                | Effect                              |
|------------------------|-------------------------------------|
| `/reset`               | Restore default categories (`config.json` via GitHub API) |
| `/clear`               | Wipe all D1 state and reset categories (confirmation required) |
| `/stats`               | Vote counts + filter status         |
| `/help`                | Show command list                   |

The **weekly reading digest** is sent automatically on Sunday (`weekly_digest.yml`); there is no on-demand `/digest` command.

## Preference filter

Votes are stored in Cloudflare D1 (the `votes` table) and used to train a TF-IDF model (sklearn) that scores future papers by `cos(paper, liked_centroid) − cos(paper, disliked_centroid)`. Papers scoring `> 0` (closer to liked than disliked) are sent.

- **Cold start**: while either side has fewer than `MIN_VOTES_PER_SIDE` (default 10) votes, the filter is disabled and every paper is sent. The score in each alert renders as `-` during this phase.
- **Score in each alert**: when the filter is active, every paper alert includes the recommender's signed relevance score (e.g. `+0.24`).
- **Vote anytime**: tap 👍 / 👎 under any paper. Votes land in D1 instantly via the Cloudflare Worker. You can re-vote on the same paper; the latest vote wins.
- **Serendipity slot**: when the filter is active, one daily slot is reserved for a near-miss paper so the feed can still surface adjacent ideas instead of collapsing too narrowly around old likes.
- **Priority mix**: each 10-paper batch targets 7 papers from priority categories (`cs.MA`, `math.NA`, `math-ph`, `physics.comp-ph`, `physics.flu-dyn`, `physics.plasm-ph`, `q-fin.CP`, `q-fin.PM`, `q-fin.TR`, `q-fin.RM`) and 3 papers from the remaining configured categories when available.

## Reading habit loop

Each paper alert carries four inline buttons:

- **👍 Like** / **👎 Dislike** — Cloudflare Worker upserts the vote into the D1 `votes` table instantly (sub-second toast).
- **Read** — forwards the paper message to the group configured by `LITFEED_TO_READ_CHAT_ID`, such as your `LitFeed - To Read` group, and marks `status=saved` in the D1 `reading_log` table. Add the bot to that group first and set `LITFEED_TO_READ_CHAT_ID` to that group's numeric chat ID.
- **Delete** — asks for confirmation, then removes the Telegram message.

The `Weekly Reading Digest` workflow runs once per week and calls `python main.py --weekly-digest` (saved counts, themes, unread queue, deep-read pick).

## Grok summaries

If `GROK_API_KEY` is configured, LitFeed asks Grok to summarize each paper that is actually sent to Telegram. Summaries are cached in the D1 `reading_log.grok_summary` column by arXiv ID, so reruns do not regenerate the same summary. If the API key is missing or the Grok request fails, the bot falls back to the normal paper message with the full abstract.

Only the chat owner (`CHAT_ID`) is authorised; commands from other users are ignored silently.

## Customising via code

Edit `main.py` defaults or tune knobs:
- `shared/default_categories.json` — category list applied on `/reset` and `/clear`.
- `LOOKBACK_HOURS` — fetch window (default 36h).
- `TELEGRAM_SAFE_MESSAGE_CHARS` — safety cap for paper messages; abstracts are otherwise shown in full.
- `GROK_DEFAULT_MODEL` — default xAI model for paper summaries.
- `MIN_VOTES_PER_SIDE` — votes needed per side before the filter activates (default 10).
- `MAX_PAPERS_PER_RUN` — total daily paper cap (default 10).
- `SERENDIPITY_SLOTS` — active-filter daily slots reserved for near-miss papers (default 1).
- `PRIORITY_CATEGORIES` / `PRIORITY_PAPERS_PER_RUN` — target mix for the final daily batch.
- `shared/topic_keywords.json` — topic labels for `/stats` (Worker) and the weekly digest (Python).

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export CHAT_ID=...
export CF_ACCOUNT_ID=... CF_D1_DATABASE_ID=... CF_D1_API_TOKEN=...
export GROK_API_KEY=...  # optional
python main.py
```

Silent exit when no papers match (no Telegram message sent).
