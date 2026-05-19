# LitFeed Cloudflare Worker

A tiny Telegram webhook receiver that powers instant button reactions for the
LitFeed bot. Replaces the 5-minute `getUpdates` poll on GitHub Actions.

## What this Worker does

When you tap a button on a paper alert in Telegram, the update arrives here in
under a second:

| Action                | Where it runs                                                    |
| --------------------- | ---------------------------------------------------------------- |
| **👍 / 👎 buttons**   | Worker answers the callback with a toast, then fires `repository_dispatch` so `votes.json` is updated in GitHub. |
| **Read** button       | Worker forwards the message to your "To Read" group, then fires `repository_dispatch` so `reading_log.json` is marked `saved` in GitHub. |
| **Delete** button     | Worker swaps the keyboard to confirm/cancel — no GitHub round-trip. |
| **Confirm delete**    | Worker calls `deleteMessage` on the Telegram API directly. |
| **Cancel**            | Worker restores the vote/Read/Delete keyboard. |
| `/list`, `/reset`, `/digest`, `/why`, `/stats`, `/help` | Worker fires `repository_dispatch`; `process_update.yml` runs `python main.py --apply-update` and commits state. |

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

### 5. (Optional) Confirm the legacy poll is dormant

`.github/workflows/poll_commands.yml` no longer runs on a cron — it's a manual
fallback only. Once the webhook is set, the `getUpdates` call inside it would
return HTTP 409 anyway, so leaving it disabled is correct.

## Tearing the Worker down

To revert to GitHub-Actions-only polling:

```bash
curl -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/deleteWebhook"
```

Then re-enable the `cron` line in `.github/workflows/poll_commands.yml` and
remove `LITFEED_DISABLE_POLL` from `daily_papers.yml` / `weekly_digest.yml`.

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
