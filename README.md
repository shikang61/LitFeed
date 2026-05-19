# LitFeed

Daily arXiv paper alerts delivered to Telegram. Runs autonomously via GitHub Actions, with optional sub-second button reactions through a Cloudflare Worker webhook.

Checks configured arXiv categories once per day and sends a Markdown-formatted message per paper, each with 👍/👎 and reading-habit buttons. Vote on papers to train a TF-IDF preference filter that progressively narrows what you see, and use the reading log to save, read, skip, and annotate papers.

## Architecture

```
Telegram ── webhook ──► Cloudflare Worker ──► repository_dispatch ──► process_update.yml
                              │                                              │
                              └─ instant: forward / delete / edit keyboard   └─ python main.py --apply-update (mutates state, commits)

GitHub Actions cron ──► daily_papers.yml ── arXiv RSS → preference filter → Telegram
GitHub Actions cron ──► weekly_digest.yml ── reading-log digest → Telegram
```

The Cloudflare Worker (see `worker/`) handles `Read` / `Delete` / 👍 / 👎
button taps in under a second by calling the Telegram Bot API directly.
State-mutating updates (vote callbacks, `Read`-button forwards, configuration
commands like `/digest`) are forwarded to GitHub via `repository_dispatch`;
`process_update.yml` runs `python main.py --apply-update` and commits the new
state files. The webhook is optional — if you skip the Worker, re-enable the
cron in `poll_commands.yml` and unset `LITFEED_DISABLE_POLL` on the other
workflows to fall back to 5-minute `getUpdates` polling.

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
3. Find the `"chat":{"id": ...}` field in the JSON. That number is your `CHAT_ID`.

### 3. Add GitHub secrets

In the repository: **Settings → Secrets and variables → Actions → New repository secret**.

| Name              | Value                                       |
|-------------------|---------------------------------------------|
| `TELEGRAM_TOKEN`  | The token from BotFather                    |
| `CHAT_ID`         | The chat ID from `getUpdates`               |
| `LITFEED_TO_READ_CHAT_ID` | Optional target group chat ID for the `Read` button |
| `GROK_API_KEY`    | Optional xAI API key for Grok summaries     |

Optionally set a repository variable named `GROK_MODEL` to override the default Grok model (`grok-4.3`).

### 4. Enable the workflow

The workflow runs daily at **08:00 UTC**. To test immediately, go to **Actions → Daily arXiv Paper Alerts → Run workflow**.

### 5. (Optional) Deploy the Cloudflare Worker for instant button reactions

Without the Worker, button taps are processed on the next 5-minute `getUpdates`
poll cycle. With the Worker, `Read` / `Delete` taps respond in under a second
and `/commands` are dispatched to GitHub Actions in real time.

Setup is documented in [`worker/README.md`](worker/README.md). High-level:

1. `cd worker && wrangler deploy`
2. `wrangler secret put` for `TELEGRAM_TOKEN`, `CHAT_ID`, `LITFEED_TO_READ_CHAT_ID`, `GITHUB_REPO`, `GITHUB_PAT`, `WEBHOOK_SECRET`.
3. `curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook -d url=<WORKER_URL> -d secret_token=<WEBHOOK_SECRET>`.

Once a webhook is set, Telegram stops delivering via `getUpdates`, so the
`poll_commands.yml` cron is intentionally disabled and the daily / weekly runs
set `LITFEED_DISABLE_POLL=1` to skip the polling step.

## Customising via Telegram

Voting and triage are done with the inline buttons under each paper alert (see *Reading habit loop* below). Configuration commands are sent directly to the bot and processed by the next workflow run (instant via the Cloudflare Worker; otherwise on the next 5-minute poll cycle).

| Command                | Effect                              |
|------------------------|-------------------------------------|
| `/list`                | Show current categories            |
| `/reset`               | Restore default categories         |
| `/digest`              | Send a weekly-style reading digest immediately |
| `/why N`               | Explain why paper `N` matched your profile |
| `/stats`               | Vote counts + filter status         |
| `/help`                | Show command list                   |

## Preference filter

Votes are stored in `votes.json` and used to train a TF-IDF model (sklearn) that scores future papers by `cos(paper, liked_centroid) − cos(paper, disliked_centroid)`. Papers scoring `> 0` (closer to liked than disliked) are sent.

- **Cold start**: while either side has fewer than `MIN_VOTES_PER_SIDE` (default 10) votes, the filter is disabled and every paper is sent. Use this phase to seed the model.
- **Vote anytime**: tap 👍 / 👎 under any paper. Votes are recorded instantly via the Cloudflare Worker (or on the next 5-minute poll if you skipped the Worker). You can re-vote on the same paper; the latest vote wins.
- **Latest batch**: each daily run is numbered (`[1]`, `[2]`, …) and the number→paper map is stored compactly in `votes.json`, so the recommender and `/why N` can reconstruct the document later.
- **Serendipity slot**: when the filter is active, one daily slot is reserved for a near-miss paper so the feed can still surface adjacent ideas instead of collapsing too narrowly around old likes.
- **Priority mix**: each 10-paper batch targets 7 papers from priority categories (`cs.MA`, `math.NA`, `math-ph`, `physics.comp-ph`, `physics.flu-dyn`, `physics.plasm-ph`, `q-fin.CP`, `q-fin.PM`, `q-fin.TR`, `q-fin.RM`) and 3 papers from the remaining configured categories when available.

## Reading habit loop

LitFeed keeps a separate `reading_log.json` state file. Each paper alert carries four inline buttons:

- **👍 Like** / **👎 Dislike** — record the vote in `votes.json`.
- **Read** — forwards the paper message to the group configured by `LITFEED_TO_READ_CHAT_ID`, such as your `LitFeed - To Read` group, and marks `status=saved` in `reading_log.json`. Add the bot to that group first, then use Telegram `getUpdates` to find the group's numeric chat ID.
- **Delete** — asks for confirmation, then removes the Telegram message.
- `/digest` sends a weekly-style digest with saved counts, recurring topics, an unread queue, and one deep-read pick.

The `Weekly Reading Digest` workflow runs once per week and calls `python main.py --weekly-digest`.

## Grok summaries

If `GROK_API_KEY` is configured, LitFeed asks Grok to summarize each paper that is actually sent to Telegram. Summaries are cached in `reading_log.json` by arXiv ID, so reruns do not regenerate the same summary. If the API key is missing or the Grok request fails, the bot falls back to the normal paper message with the full abstract.

Only the chat owner (`CHAT_ID`) is authorised; commands from other users are ignored silently.

## Customising via code

Edit `main.py` defaults or tune knobs:
- `DEFAULT_CATEGORIES` — applied on `/reset` and when `config.json` is missing.
- `LOOKBACK_HOURS` — fetch window (default 26h, matches once-daily cron with drift margin).
- `TELEGRAM_SAFE_MESSAGE_CHARS` — safety cap for paper messages; abstracts are otherwise shown in full.
- `GROK_DEFAULT_MODEL` — default xAI model for paper summaries.
- `MIN_VOTES_PER_SIDE` — votes needed per side before the filter activates (default 10).
- `MAX_PAPERS_PER_RUN` — total daily paper cap (default 10).
- `SERENDIPITY_SLOTS` — active-filter daily slots reserved for near-miss papers (default 1).
- `PRIORITY_CATEGORIES` / `PRIORITY_PAPERS_PER_RUN` — target mix for the final daily batch.
- `TOPIC_KEYWORDS` — lightweight topic labels shown in `/stats` and the weekly digest.

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export CHAT_ID=...
export GROK_API_KEY=...  # optional
python main.py
```

Silent exit when no papers match (no Telegram message sent).
