# LitFeed Cloudflare Worker

A Telegram webhook receiver — the **only** update consumer for LitFeed in
production. Handles all button taps and owner commands at the edge. GitHub
Actions only runs the daily arXiv batch and the Sunday weekly digest.

For an end-to-end walkthrough, see [`../docs/architecture.md`](../docs/architecture.md).

## What this Worker does

When you tap a button on a paper alert in Telegram, the update arrives here in
under a second:

| Action                | Where it runs                                                    |
| --------------------- | ---------------------------------------------------------------- |
| **👍 / 👎 buttons**   | Worker toast + D1 `votes` upsert. |
| **Read** button       | Forward to To Read group + D1 `reading_log.status='saved'`. |
| **Delete** button     | Confirm/cancel keyboard + `deleteMessage`. |
| `/stats`, `/help`     | D1 reads + `sendMessage`. |
| `/clear`              | Confirmation UI; confirm wipes D1 and resets categories. |
| `/reset`              | Resets `config.json` via GitHub Contents API. |

There is no `/digest` command — the weekly digest is sent by `weekly_digest.yml`
only.

## One-time setup

### 1. Install Wrangler and deploy

```bash
cd worker
npm install -g wrangler   # or: npx wrangler ...
wrangler login
wrangler deploy
```

The first `wrangler deploy` prints a URL like
`https://litfeed-bot.<your-subdomain>.workers.dev`. Save it.

### 2. Create a GitHub PAT for the Worker

`/reset` and confirmed `/clear` update `config.json` in the repo via the
[Contents API](https://docs.github.com/en/rest/repos/contents). Create a
**fine-grained personal access token** at
<https://github.com/settings/tokens?type=beta>:

- Repository access: only this repo.
- Repository permissions: **Contents → Read and write**.

(A classic PAT with the `repo` scope also works.)

### 3. Set Worker secrets

Pick a long random string for `WEBHOOK_SECRET` (Telegram → Worker auth). Then:

```bash
cd worker
wrangler secret put TELEGRAM_TOKEN          # paste BotFather token
wrangler secret put CHAT_ID                 # your numeric chat id
wrangler secret put LITFEED_TO_READ_CHAT_ID # destination group chat id
wrangler secret put GITHUB_REPO             # e.g. shikang61/LitFeed
wrangler secret put GITHUB_PAT              # the PAT from step 2
wrangler secret put WEBHOOK_SECRET          # the random string
```

### 4. Tell Telegram about the webhook

```bash
WORKER_URL="https://litfeed-bot.<your-subdomain>.workers.dev"
TELEGRAM_TOKEN="123456:ABC-DEF..."
WEBHOOK_SECRET="<the same random string from step 3>"

curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
  -d "url=${WORKER_URL}" \
  -d "secret_token=${WEBHOOK_SECRET}" \
  -d 'allowed_updates=["message","edited_message","callback_query"]'
```

Verify:

```bash
curl -fsS "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getWebhookInfo"
```

Topic labels for `/stats` come from `shared/topic_keywords.json` (same file
the weekly digest uses in Python). Default categories for `/reset` live in
`shared/default_categories.json`.

## Tearing the Worker down

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/deleteWebhook"
```

Telegram commands and buttons stop working until you set a webhook again.

## Local development

```bash
cd worker
wrangler dev   # serves on http://127.0.0.1:8787
```

For local secrets, drop them in `worker/.dev.vars` (gitignored):

```
TELEGRAM_TOKEN=...
CHAT_ID=...
LITFEED_TO_READ_CHAT_ID=...
GITHUB_REPO=...
GITHUB_PAT=...
WEBHOOK_SECRET=...
```

Use [ngrok](https://ngrok.com/) or `cloudflared tunnel` if you want Telegram to
reach your laptop.

## Cost

Cloudflare Workers free tier: 100,000 requests/day. A personal LitFeed bot uses
well under 0.1% of that.
