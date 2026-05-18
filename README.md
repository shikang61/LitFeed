# LitFeed

Daily arXiv paper alerts delivered to Telegram. Runs autonomously via GitHub Actions.

Checks configured arXiv categories once per day and sends a Markdown-formatted message per paper, each with 👍/👎 and reading-habit buttons. Vote on papers to train a TF-IDF preference filter that progressively narrows what you see, and use the reading log to save, read, skip, and annotate papers.

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

### 4. Enable the workflow

The workflow runs daily at **08:00 UTC**. To test immediately, go to **Actions → Daily arXiv Paper Alerts → Run workflow**.

## Customising via Telegram

Send commands directly to your bot. They are processed at the start of the next scheduled run (max latency ≈ 12h with the default twice-daily cron), then `config.json` is committed back to the repo by the workflow.

| Command                | Effect                              |
|------------------------|-------------------------------------|
| `/list`                | Show current categories            |
| `/add_cat <arxiv.cat>` | Add arXiv category (e.g. `cs.LG`)   |
| `/rm_cat <arxiv.cat>`  | Remove arXiv category               |
| `/reset`               | Restore default categories         |
| `/like N [N …]`        | Like papers by number from the latest batch |
| `/dislike N [N …]`     | Dislike papers by number from the latest batch |
| `/later N [N …]`       | Save papers for later reading |
| `/read N [N …]`        | Mark papers as read |
| `/skip N [N …]`        | Skip papers and train them as negative examples |
| `/note N <text>`       | Attach a note to a paper from the latest batch |
| `/queue`               | Show saved/unread papers |
| `/notes [query]`       | Search paper notes |
| `/digest`              | Send a weekly-style reading digest immediately |
| `/stats`               | Vote counts + filter status         |
| `/help`                | Show command list                   |

## Preference filter

Each paper message includes 👍/👎 buttons. Votes are stored in `votes.json` and used to train a TF-IDF model (sklearn) that scores future papers by `cos(paper, liked_centroid) − cos(paper, disliked_centroid)`. Papers scoring `> 0` (closer to liked than disliked) are sent.

- **Cold start**: while either side has fewer than `MIN_VOTES_PER_SIDE` (default 10) votes, the filter is disabled and every paper is sent. Use this phase to seed the model.
- **Vote anytime**: the poll workflow (every 5 min) records votes via Telegram callbacks. You can re-vote on the same paper; the latest vote wins.
- **Batch vote**: each paper in the daily run is numbered (`[1]`, `[2]`, …). Reply with `/like 1 3 5` and/or `/dislike 2 4` to vote on several at once in one message — the numbers refer to the most recent batch. Faster than tapping each paper's button, and one message = one poll cycle.
- **Latest batch**: the most recent batch is stored compactly in `votes.json` so batch commands and callback buttons can reconstruct the document for training.
- **Serendipity slot**: when the filter is active, one daily slot is reserved for a near-miss paper so the feed can still surface adjacent ideas instead of collapsing too narrowly around old likes.

## Reading habit loop

LitFeed keeps a separate `reading_log.json` state file. Daily paper messages include `Read later`, `Read`, and `Skip` buttons:

- `Read later` saves a paper to your queue.
- `Read` marks it as read.
- `Skip` marks it as skipped and records it as a negative training example.
- `/note N <text>` adds a lightweight literature note to paper `N` from the latest batch.
- `/queue` and `/notes [query]` retrieve your saved papers and notes from Telegram.
- `/digest` sends a weekly-style digest with saved/read/skipped counts, recurring topics, an unread queue, and one deep-read pick.

The `Weekly Reading Digest` workflow runs once per week and calls `python main.py --weekly-digest`.

Only the chat owner (`CHAT_ID`) is authorised; commands from other users are ignored silently.

## Customising via code

Edit `main.py` defaults or tune knobs:
- `DEFAULT_CATEGORIES` — applied on `/reset` and when `config.json` is missing.
- `LOOKBACK_HOURS` — fetch window (default 26h, matches once-daily cron with drift margin).
- `SNIPPET_CHARS` — abstract preview length in the Telegram message.
- `MIN_VOTES_PER_SIDE` — votes needed per side before the filter activates (default 10).
- `MAX_PAPERS_PER_RUN` — total daily paper cap (default 5).
- `SERENDIPITY_SLOTS` — active-filter daily slots reserved for near-miss papers (default 1).
- `TOPIC_KEYWORDS` — lightweight topic labels shown in `/stats` and the weekly digest.

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export CHAT_ID=...
python main.py
```

Silent exit when no papers match (no Telegram message sent).
