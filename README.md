# LitFeed

Daily arXiv paper alerts delivered to Telegram. Runs autonomously via GitHub Actions.

Checks configured arXiv categories once per day and sends a Markdown-formatted message per paper, each with 👍/👎 buttons. Vote on papers to train a TF-IDF preference filter that progressively narrows what you see.

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
| `/vote`                | Batch-vote recent papers via a keyboard |
| `/stats`               | Vote counts + filter status         |
| `/help`                | Show command list                   |

## Preference filter

Each paper message includes 👍/👎 buttons. Votes are stored in `votes.json` and used to train a TF-IDF model (sklearn) that scores future papers by `cos(paper, liked_centroid) − cos(paper, disliked_centroid)`. Papers scoring `> 0` (closer to liked than disliked) are sent.

- **Cold start**: while either side has fewer than `MIN_VOTES_PER_SIDE` (default 10) votes, the filter is disabled and every paper is sent. Use this phase to seed the model.
- **Vote anytime**: the poll workflow (every 5 min) records votes via Telegram callbacks. You can re-vote on the same paper; the latest vote wins.
- **Batch vote**: `/vote` sends one message with a toggle button per recent unvoted paper (tap cycles ⚪→👍→👎) plus a Submit button that records the whole batch at once. Faster than tapping each paper's message — though, like all callbacks, each tap takes up to one poll cycle (~5 min) to register.
- **Cache**: last 500 sent papers' text is cached in `votes.json` so callback handlers can reconstruct the document for training. Voting on older papers (beyond the cache) is rejected with a toast.

Only the chat owner (`CHAT_ID`) is authorised; commands from other users are ignored silently.

## Customising via code

Edit `main.py` defaults or tune knobs:
- `DEFAULT_CATEGORIES` — applied on `/reset` and when `config.json` is missing.
- `LOOKBACK_HOURS` — fetch window (default 26h, matches once-daily cron with drift margin).
- `SNIPPET_CHARS` — abstract preview length in the Telegram message.
- `MIN_VOTES_PER_SIDE` — votes needed per side before the filter activates (default 10).
- `MAX_SENT_CACHE` — paper-text cache size for vote callbacks (default 500).

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export CHAT_ID=...
python main.py
```

Silent exit when no papers match (no Telegram message sent).
