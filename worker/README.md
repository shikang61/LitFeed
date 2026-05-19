# LitFeed Cloudflare Worker

A Telegram webhook receiver — the **only** update consumer for LitFeed in
production. Handles instant button taps and lightweight commands at the edge;
dispatches heavier work to GitHub Actions.

For an end-to-end walkthrough of how this Worker plugs into GitHub Actions and
why the request flow is split into instant vs. dispatched paths, see
[`../docs/architecture.md`](../docs/architecture.md).

## What this Worker does

When you tap a button on a paper alert in Telegram, the update arrives here in
under a second:

| Action                | Where it runs                                                    |
| --------------------- | ---------------------------------------------------------------- |
| **👍 / 👎 buttons**   | Worker answers the callback with a toast, then upserts the vote into the D1 `votes` table directly (no GitHub round-trip). |
| **Read** button       | Worker forwards the message to your "To Read" group, then upserts `status='saved'` into the D1 `reading_log` table directly. |
| **Delete** button     | Worker swaps the keyboard to confirm/cancel — no GitHub round-trip. |
| **Confirm delete**    | Worker calls `deleteMessage` on the Telegram API directly. |
| **Cancel**            | Worker restores the vote/Read/Delete keyboard. |
| `/stats`, `/help` | Worker reads D1 and replies instantly (no GitHub run). |
| `/digest` | Worker sends “Generating digest…”, then dispatches; `main.py --apply-update` builds the digest from D1. |
| `/reset`, `/clear` | Worker dispatches; `process_update.yml` runs `main.py` and may commit `config.json`. |

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

The Worker needs to fire `repository_dispatch` events on your repo. Create a
**fine-grained personal access token** at
<https://github.com/settings/tokens?type=beta>:

- Resource owner: yourself.
- Repository access: only this repo.
- Repository permissions: **Contents → Read and write**.

Copy the token. (A classic PAT with the `repo` scope also works, but the
fine-grained one is tighter.)

### 3. Set Worker secrets

Pick a long random string for `WEBHOOK_SECRET` (it authenticates Telegram → your
Worker; nobody else needs to know it). Then:

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

Telegram only sends to one consumer at a time — webhook **or** `getUpdates`,
not both. Point Telegram at the Worker:

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

You should see your Worker URL in `result.url` and `pending_update_count: 0`.

Topic labels for `/stats` come from `shared/topic_keywords.json` (same file
the weekly digest uses in Python).

## Tearing the Worker down

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/deleteWebhook"
```

Telegram commands and buttons will stop working until you set a webhook again.
There is no `getUpdates` fallback in `main.py`.

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

Cloudflare Workers free tier: 100,000 requests/day and 10ms CPU/request. A
personal LitFeed bot uses well under 0.1% of that.
