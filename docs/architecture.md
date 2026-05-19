# How the Cloudflare Worker works

LitFeed used to receive Telegram updates by polling the Bot API on a 5-minute
GitHub Actions cron. Button taps therefore took up to 5 minutes to react. To
make UI-only actions (vote, forward, delete) feel instant while keeping all
state in `votes.json` / `reading_log.json` / `config.json` in this repo, the
project now runs a tiny Cloudflare Worker as a Telegram webhook.

## The 30-second version

A Cloudflare Worker is a JavaScript program that Cloudflare runs at the edge
of their network — i.e. in a data center close to the request — within
~5ms cold start. `worker/index.js` is one file (~250 lines) executed every
time a request comes in.

Telegram's bot API supports two delivery models:

- **Polling** (`getUpdates`) — your code calls Telegram every N seconds
  asking "anything new?" That's what GitHub Actions did before, on a
  5-min cron.
- **Webhooks** — Telegram instantly POSTs each new event to a URL of your
  choice. That URL is now the Worker.

The two modes are mutually exclusive (Telegram won't deliver to both), so
`poll_commands.yml`'s cron is disabled and `LITFEED_DISABLE_POLL=1` is set
on the daily/weekly workflows.

## End-to-end request flow

```
You tap a button in Telegram
        │
        ▼
Telegram servers
        │ POST  https://litfeed-bot.litfeed.workers.dev
        │       Body: {update_id, callback_query: {id, data, message, from, …}}
        │       Header: X-Telegram-Bot-Api-Secret-Token: <WEBHOOK_SECRET>
        ▼
Cloudflare's edge network
        │ Spins up your Worker in a V8 isolate (~5ms)
        │ Calls export default.fetch(request, env, ctx)
        ▼
worker/index.js
        │ 1. Verify the secret header matches WEBHOOK_SECRET → 403 if not
        │ 2. Verify sender id == CHAT_ID → silently ignore otherwise
        │ 3. Branch on update type
        ▼
        ┌───────── Instant path ─────────┐    ┌──── Dispatched path ────┐
        │ v:like / v:dislike             │    │ Anything else           │
        │   answerCallbackQuery (toast)  │    │ /commands etc.          │
        │   ctx.waitUntil(dispatch…)     │    │                         │
        │                                │    │                         │
        │ h:read_to_group                │    │                         │
        │   forwardMessage to To Read    │    │                         │
        │   answerCallbackQuery          │    │                         │
        │   ctx.waitUntil(dispatch…)     │    │                         │
        │                                │    │                         │
        │ h:delete                       │    │                         │
        │   editMessageReplyMarkup       │    │                         │
        │   answerCallbackQuery          │    │                         │
        │ h:confirm_delete               │    │                         │
        │   deleteMessage                │    │                         │
        │ h:cancel_delete                │    │                         │
        │   editMessageReplyMarkup       │    │                         │
        └────────────────────────────────┘    └─────────────────────────┘
              │                                         │
              ▼                                         ▼
        Telegram sees toast / message change       dispatchToGitHub(env, update, false)
        within ~500ms                               POST /repos/<owner>/<repo>/dispatches
                                                    Authorization: Bearer <GITHUB_PAT>
                                                    Body: {event_type:"telegram-update",
                                                           client_payload:{update,
                                                                           webhook_handled}}
                                                          │
                                                          ▼
                                                    GitHub kicks off
                                                    .github/workflows/process_update.yml
                                                          │
                                                          ▼
                                                    runner: ubuntu-latest
                                                    checkout · install · run:
                                                      python main.py --apply-update
                                                      with the JSON payload
                                                          │
                                                          ▼
                                                    main.py mutates votes.json /
                                                    config.json / reading_log.json
                                                    skips Telegram calls because
                                                    webhook_handled=True
                                                          │
                                                          ▼
                                                    git commit -am "…"; git push
                                                    (~25-45s end-to-end)
```

## Why the two paths

The whole point of the Worker is **the latency split**.

Things in the **instant path** are pure Telegram-API calls — no state
files, no recommender. The Worker can finish them in one HTTP round-trip to
`api.telegram.org`. That's why deletes feel instantaneous.

Things in the **dispatched path** mutate `votes.json` or `reading_log.json`,
which live in this repo. Workers are ephemeral, can't checkout the repo,
can't commit, so we hand off to GitHub Actions. That takes ~30s for runner
spin-up + checkout + push. The user-facing toast is already shown by then;
the state catches up in the background.

For votes (`v:like` / `v:dislike`) we want both: the toast must show
instantly, AND `votes.json` must record the vote. So the Worker does both —
answers the callback, then `ctx.waitUntil(dispatchToGitHub(env, update, true))`
queues the dispatch and lets the Worker return without blocking on it. The
`webhook_handled=true` flag tells `main.py` "the toast/UI was already done,
only mutate state."

## Concrete examples

### Tap **Delete**

1. Telegram POSTs to Worker with `callback_query.data = "h:delete:abc123"`.
2. Worker's `handleCallback` matches `kind="h", action="delete"`.
3. Worker calls Telegram `editMessageReplyMarkup` to swap the keyboard to
   Confirm/Cancel.
4. Worker calls `answerCallbackQuery` with toast `"Confirm deletion?"`.
5. Worker returns 200 OK to Telegram. Done. ~300ms total.
6. **No** GitHub Actions run — this never touched any state file.

### Tap **👍 Like**

1. Telegram POSTs `callback_query.data = "v:like:abc123"`.
2. Worker matches `kind="v", action="like"`.
3. Worker calls `answerCallbackQuery` with toast `"Recorded 👍"`. Visible
   in ~300ms.
4. Worker calls `ctx.waitUntil(dispatchToGitHub(...))`. This tells
   Cloudflare "keep me alive long enough to fire one more HTTP request to
   GitHub, but you can return to Telegram now." The dispatch HTTP call to
   `api.github.com` happens in parallel with the response to Telegram.
5. GitHub receives the dispatch, queues `process_update.yml`.
6. Workflow runs: checks out repo, installs deps, runs
   `python main.py --apply-update` with the JSON payload. `main.py` calls
   `_record_vote(votes, key, text, "like")`, writes `votes.json`, commits,
   pushes. ~25-45s after step 1.

### Send `/digest`

1. Telegram POSTs `message.text = "/digest"`.
2. Worker doesn't match any callback handler (no `cb`).
3. Falls through to `ctx.waitUntil(dispatchToGitHub(env, update, false))`.
   Returns 200 OK.
4. Workflow runs `main.py --apply-update`, which calls
   `handle_command("/digest", ...)`, builds the digest text, and sends it
   via Telegram. ~30s later you see the digest message.

## The infrastructure itself

- **No server to babysit.** The Worker is pay-per-request (Cloudflare's
  free tier covers 100k req/day, which this bot will never approach). No
  autoscaling, no idling servers.
- **V8 isolates, not containers.** Each request runs in a V8 isolate that
  boots in milliseconds and shares memory with no one. Way cheaper than
  containers — that's why cold starts feel instant.
- **Code lives at the edge.** `index.js` is replicated globally; whichever
  data center catches the Telegram POST runs it locally.
- **Secrets are encrypted at rest.** The `wrangler secret put` calls store
  `TELEGRAM_TOKEN`, `GITHUB_PAT`, etc. encrypted in Cloudflare's KV,
  decrypted only inside the running isolate. They never appear in your code.
- **Logs.** Run `npx wrangler@latest tail` from `worker/` and you'll see
  live `console.log` output for every request hitting your Worker. Useful
  for debugging.

## Why this design over alternatives

- **vs. running a server** — you'd need a 24/7 process, autoscaling,
  monitoring, TLS, deployment. Worker is one file and one `wrangler deploy`.
- **vs. polling more frequently** — GitHub Actions has a 1-minute cron
  minimum; even at 1 min the worst-case latency is 60s and you waste CI
  minutes 24/7.
- **vs. doing everything in the Worker** — Workers can't read/write your
  repo. You'd need an external database (KV, D1, etc.), and then state
  isn't in `votes.json` anymore and the Python recommender can't read it.

The hybrid keeps the simplicity of "state files in git" while getting the
snappiness of an always-on edge service for UI-only actions.

## Debugging tips

- `npx wrangler@latest tail` (run inside `worker/`) — live request logs.
- `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo` — confirm the
  webhook URL is set and shows recent delivery state, including
  `last_error_message` if Telegram is failing to reach the Worker.
- GitHub repo → **Actions → "Process Telegram Update"** — every dispatched
  action shows up here as a workflow run; click in to see the full log.
