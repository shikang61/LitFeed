# LitFeed

Daily arXiv paper alerts delivered to Telegram. Runs autonomously via GitHub Actions.

Checks four arXiv categories (`physics.plasm-ph`, `physics.comp-ph`, `q-fin.CP`, `q-fin.PR`) once per day and sends a Markdown-formatted message per paper.

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
| `/help`                | Show command list                   |

Only the chat owner (`CHAT_ID`) is authorised; commands from other users are ignored silently.

## Customising via code

Edit `main.py` defaults or tune knobs:
- `DEFAULT_CATEGORIES` — applied on `/reset` and when `config.json` is missing.
- `LOOKBACK_HOURS` — fetch window (default 12h, matches twice-daily cron).
- `SNIPPET_CHARS` — abstract preview length in the Telegram message.

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export CHAT_ID=...
python main.py
```

Silent exit when no papers match (no Telegram message sent).
