# LitFeed architecture

End-to-end picture of how LitFeed works today: paper fetching, the Telegram
webhook, the four triggers that make the bot do anything, and exactly which
D1 row gets read or written at each step.

For the historical context — why we moved from JSON-in-git to D1 — see
[d1_migration.md](d1_migration.md). For the scoring algorithm (TF-IDF
model, selection pipeline, tunable knobs, ideas for improvement) see
[recommender.md](recommender.md). For Worker setup, see
[../worker/README.md](../worker/README.md).

## 1. Components at a glance

```
                ┌──────────────────────────────────────────────┐
                │   GitHub Actions (Python 3.11, main.py)      │
                │                                              │
                │   • daily_papers.yml  (paper fetch + alert)  │
                │   • weekly_digest.yml (Sun 18:00 UTC)        │
                │   • process_update.yml (/commands handler)   │
                └────────────┬─────────────────────┬───────────┘
                             │                     │
                  REST writes│                     │repository_dispatch
                             │                     │
                             ▼                     ▼
                     ┌─────────────────┐   ┌──────────────────┐
       arXiv RSS  ──►│                 │   │  cron-job.org    │
                     │  Cloudflare D1  │   │  hits dispatch   │
                     │  (litfeed_state)│   │  to fire daily   │
                     │                 │   └──────────────────┘
                     └────────▲────────┘
                              │  native binding (env.DB)
                              │
                     ┌────────┴────────────────────────────┐
                     │   Cloudflare Worker (index.js)      │
                     │   Telegram webhook receiver         │
                     │   • instant: votes, read, delete    │
                     │   • dispatch: /commands             │
                     └────────▲────────────────────────────┘
                              │ HTTPS POST
                              ▼
                       ┌──────────────┐
                       │   Telegram   │
                       └──────┬───────┘
                              │
                              ▼
                          You / chat
```

Nothing in this picture talks directly to another piece without going via
either Telegram, GitHub's `repository_dispatch` event, or D1.

## 2. Where state lives

| State                         | Storage                       | Mutated by                                     |
|-------------------------------|-------------------------------|------------------------------------------------|
| `categories`                  | `config.json` (in git)        | `/reset` (rare; rewrites file)                 |
| Liked / disliked papers       | D1 `votes`                    | Worker (`v:like`, `v:dislike`)                 |
| Reading log (sent + saved)    | D1 `reading_log`              | `main.py` (sends), Worker (`Read` button)      |
| Sent-id ring buffer (dedup)   | D1 `sent_ids` (capped at 500) | `main.py` daily run                            |
| Most recent batch (positions) | D1 `last_batch`               | `main.py` daily run (wholesale-replaced)       |
| Telegram offset               | D1 `kv` row `last_update_id`  | `main.py --apply-update`                       |

`config.json` is the **only** thing the app writes back to the repo, and
only when `/reset` actually changes the category list. Daily runs, votes,
button taps, the weekly digest, and even most `/commands` produce **zero
git diff**.

### 2.1 D1 schema

Defined in `migrations/0001_init.sql`. Five tables, all with single-row
upserts (`INSERT … ON CONFLICT DO UPDATE`) so concurrent writes never race:

```sql
votes        (paper_key PK, bucket, text, ts)
reading_log  (paper_key PK, title, url, text, categories, score,
              status, status_ts, sent_ts, created_ts, grok_summary)
sent_ids     (paper_key PK, sent_ts)
last_batch   (position PK, paper_key, text, score)
kv           (key PK, value)
```

- **`votes`** — one row per paper *currently* in the liked/disliked corpus.
  Re-voting upserts; we don't keep history. The `text` column is the
  abstract, used to fit the TF-IDF recommender.
- **`reading_log`** — one row per paper we've ever sent or saved. The
  `text` column lets the Worker look up vote text without carrying the
  abstract in `callback_data` (Telegram caps that at 64 bytes).
- **`sent_ids`** — dedup ring buffer for arXiv short IDs (version stripped,
  so `2401.12345v1` and `v2` don't both alert). Trimmed to 500 newest each
  daily run.
- **`last_batch`** — positions 1..N for the most recent paper batch. Used
  by Worker callback votes to look up paper text when the paper isn't
  in `reading_log` yet. Wholesale-replaced each daily run.
- **`kv`** — currently only holds `last_update_id` (Telegram offset, kept
  for legacy reasons since the webhook is the live consumer).

### 2.2 How Python talks to D1

`state_store.py` is the only file that writes to D1 from Python. It uses
`_d1.py`, a thin REST client around Cloudflare's `/d1/database/{id}/query`
endpoint, configured by three env vars:

```
CF_ACCOUNT_ID       Cloudflare account id
CF_D1_DATABASE_ID   D1 database id
CF_D1_API_TOKEN     API token with D1:Edit
```

D1 is mandatory: without those env vars, `_d1.query()` raises
`_d1.D1Error` on the first call. There is no JSON fallback — that was
removed in Phase F of the migration.

Workers don't use REST. They have a **native binding** declared in
`worker/wrangler.toml`:

```toml
[[d1_databases]]
binding = "DB"
database_name = "litfeed_state"
database_id = "…"
```

and call it as `env.DB.prepare("…").bind(…).run()`. Much faster than REST
(no HTTP overhead) and runs in the same V8 isolate as the rest of the
Worker.

## 3. The four triggers

Everything LitFeed does is initiated by one of these four events.

### 3.1 Daily paper alert  (cron-job.org → `daily_papers.yml` → D1)

Fires once per scheduled time (cron-job.org POSTs a `repository_dispatch`
of type `run-paper-alerter`). Steps:

```
1. checkout repo (config.json — for categories only)
2. python main.py
   │
   ├─ state_store.load_config()
   │    → reads config.json (categories)
   │    → D1: SELECT value FROM kv WHERE key='last_update_id'
   │    → D1: SELECT paper_key FROM sent_ids ORDER BY sent_ts ASC
   │
   ├─ state_store.load_votes()
   │    → D1: SELECT paper_key, bucket, text, ts FROM votes
   │    → D1: SELECT position, paper_key, text, score FROM last_batch
   │
   ├─ state_store.load_reading_log()
   │    → D1: SELECT * FROM reading_log
   │
   ├─ getUpdates is skipped (LITFEED_DISABLE_POLL=1; the webhook owns
   │    Telegram's update queue)
   │
   ├─ fetch arXiv RSS for each category, dedup against sent_ids,
   │   drop > LOOKBACK_HOURS, score with recommender (if warm),
   │   apply diversity + per-category cap + priority mix
   │
   └─ for each survivor paper p:
         sendMessage to Telegram (with vote keyboard)
         record_sent_paper(p) → D1:
             INSERT INTO reading_log (paper_key, title, url, text,
               categories, score, status='sent', sent_ts, created_ts)
             ON CONFLICT(paper_key) DO UPDATE SET …
       after loop:
         cfg["sent_ids"] += newly_sent  (capped at 500)
         save_config(cfg) → D1: rewrite sent_ids table; set kv.last_update_id
         save_votes(votes) → D1: DELETE FROM last_batch; INSERT new positions
         (save_reading_log is a no-op; per-paper upserts above already wrote)
```

Net result on D1: each new paper produces one `reading_log` row + one
`sent_ids` row; `last_batch` is rebuilt; `kv.last_update_id` and the trim
of `sent_ids` keep things bounded.

Net result on the repo: **no commit**. Permission is `contents: read`.

### 3.2 Vote / Read button tap  (Telegram → Worker → D1, no Python)

By far the most common trigger. Latency is sub-second because GitHub is
not involved.

```
You tap 👍 in Telegram
        │
        ▼
Telegram POSTs to https://litfeed-bot.litfeed.workers.dev
        │       Body: {update_id, callback_query: {id, data:"v:like:2401.12345", …}}
        │       Header: X-Telegram-Bot-Api-Secret-Token: <WEBHOOK_SECRET>
        ▼
Cloudflare Worker (V8 isolate, ~5ms cold start)
        │
        ├─ verify WEBHOOK_SECRET → 403 if mismatch
        ├─ verify sender id == CHAT_ID → silently 200 otherwise
        ├─ parse callback_data → kind="v", action="like", key="2401.12345"
        │
        ├─ tg.answerCallbackQuery(id, "Recorded 👍")        ← toast in ~300ms
        │
        └─ ctx.waitUntil(recordVote(env, key, "liked"))
              env.DB.prepare(`
                INSERT INTO votes (paper_key, bucket, text, ts)
                VALUES (?1, ?2, COALESCE(
                  (SELECT text FROM reading_log WHERE paper_key = ?1),
                  (SELECT text FROM last_batch  WHERE paper_key = ?1),
                  ''
                ), ?3)
                ON CONFLICT(paper_key) DO UPDATE SET
                  bucket = excluded.bucket, ts = excluded.ts
              `).bind(key, "liked", now).run()
```

`text` is filled by D1 itself via `COALESCE`, so the Worker never has to
carry the abstract through Telegram's 64-byte `callback_data` limit.

**Read button** is the same pattern with one extra step: forward the
message to the To Read group first, then upsert `reading_log.status =
'saved'`. **Delete** just edits / deletes the Telegram message; no D1
write.

Net result: zero GitHub Actions runs, zero git commits, one D1 upsert.

### 3.3 Telegram command  (Telegram → Worker → `process_update.yml` → D1)

For things the Worker can't (or won't) do at the edge: `/reset`,
`/digest`, `/stats`, `/help`. These need the full Python environment
(multi-line Markdown, sklearn, `config.json` mutation).

```
You send /digest
        │
        ▼
Telegram POSTs the update
        ▼
Worker (handleCallback returns false → falls through)
        │
        └─ ctx.waitUntil(dispatchToGitHub(env, update, false))
              POST https://api.github.com/repos/<owner>/LitFeed/dispatches
              Body: {event_type:"telegram-update",
                     client_payload:{update, webhook_handled:false}}
        ▼
GitHub Actions runs process_update.yml
        │
        └─ python main.py --apply-update
              LITFEED_UPDATE_JSON = <the update payload>
              LITFEED_WEBHOOK_HANDLED = "false"
              │
              ├─ load_config / load_votes / load_reading_log
              │   (all from D1 as in §3.1)
              │
              ├─ apply_webhook_update(...) → handle_command("/digest", ...)
              │   builds the digest text from votes + reading_log
              │   sends it via Telegram
              │
              └─ save_config(cfg)
                  → D1: kv.last_update_id = update_id of this update
                  → D1: (no sent_ids change for /digest, so the row set is the same)
                  → config.json: rewritten only if categories changed (i.e. /reset)
```

The `Commit state changes` step at the end runs `safe_state_push.sh`,
which only touches `config.json` and exits cleanly when there's nothing
to commit. So `/digest`, `/stats`, `/help`, etc. all produce zero commits;
only `/reset` (which actually changes categories) produces a commit.

### 3.4 Weekly digest  (Sun 18:00 UTC cron → `weekly_digest.yml` → D1 read)

```
1. cron fires weekly_digest.yml
2. python main.py --weekly-digest
   │
   ├─ load_votes()        → reads D1 (votes + last_batch)
   ├─ load_reading_log()  → reads D1 (reading_log)
   ├─ format_weekly_digest(votes, reading_log)
   └─ sendMessage to Telegram
```

Permission is `contents: read`. No state mutations, no commits — it's a
pure read-and-send.

## 4. D1 read/write map (one-glance)

| Table         | Daily run               | Vote tap       | Read tap        | `/digest`        | `/reset`         | Weekly |
|---------------|-------------------------|----------------|-----------------|------------------|------------------|--------|
| `votes`       | read                    | **upsert row** | —               | read             | —                | read   |
| `reading_log` | **upsert per paper**    | read for text  | **upsert row**  | read             | —                | read   |
| `sent_ids`    | **rebuild (capped 500)**| —              | —               | read             | —                | —      |
| `last_batch`  | **wholesale replace**   | read for text  | —               | —                | —                | —      |
| `kv`          | **set last_update_id**  | —              | —               | **set**          | **set**          | —      |
| `config.json` | —                       | —              | —               | —                | **rewrite**      | —      |

"upsert row" means a single `INSERT … ON CONFLICT DO UPDATE`. Two
concurrent writes to the same row serialise with last-write-wins, which is
what we want: a flipped 👍 → 👎 should follow the latest action.

## 5. Cloudflare Worker (deep dive)

This section preserves the prose that used to be all of `architecture.md`.
It explains *why* the Worker exists and how the JS code in
`worker/index.js` is structured.

### The 30-second version

A Cloudflare Worker is a JavaScript program that Cloudflare runs at the
edge of their network — i.e. in a data center close to the request —
within ~5ms cold start. `worker/index.js` is one file (~250 lines)
executed every time a request comes in.

Telegram's bot API supports two delivery models:

- **Polling** (`getUpdates`) — your code calls Telegram every N seconds
  asking "anything new?" That's what GitHub Actions did before, on a
  5-min cron.
- **Webhooks** — Telegram instantly POSTs each new event to a URL of your
  choice. That URL is now the Worker.

The two modes are mutually exclusive (Telegram won't deliver to both), so
`poll_commands.yml`'s cron is disabled and `LITFEED_DISABLE_POLL=1` is set
on the daily/weekly workflows.

### End-to-end request flow

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
        ┌──────── Instant path (no GitHub) ────────┐  ┌─ Dispatched path ─┐
        │ v:like / v:dislike                       │  │ /commands         │
        │   answerCallbackQuery (toast)            │  │   (only path that │
        │   ctx.waitUntil(recordVote → D1)         │  │    still needs    │
        │                                          │  │    main.py)       │
        │ h:read_to_group                          │  │                   │
        │   forwardMessage to To Read              │  │                   │
        │   answerCallbackQuery                    │  │                   │
        │   ctx.waitUntil(recordReadSaved → D1)    │  │                   │
        │                                          │  │                   │
        │ h:delete / h:confirm_delete /            │  │                   │
        │ h:cancel_delete                          │  │                   │
        │   editMessageReplyMarkup / deleteMessage │  │                   │
        └──────────────────────────────────────────┘  └───────────────────┘
              │                                                │
              ▼                                                ▼
        Telegram sees toast / message change             dispatchToGitHub(env, update, false)
        within ~500ms; D1 row upserts in ~ms             POST /repos/<owner>/<repo>/dispatches
                                                         Body: {event_type:"telegram-update",
                                                                client_payload:{update,
                                                                                webhook_handled:false}}
                                                               │
                                                               ▼
                                                         process_update.yml
                                                         runs python main.py --apply-update
                                                         which talks to D1 via REST
```

After the migration only `/commands` (`/reset`, `/digest`, `/stats`,
`/help`) still go via GitHub Actions — those mutate `config.json` or
want to send a multi-line Telegram message that's easier to assemble in
Python. Everything else (votes, reads, deletes) is Worker-only.

### Why the two paths

The whole point of the Worker is **the latency split**.

Things in the **instant path** are pure Telegram-API calls plus a D1
upsert — both happen at the edge in single-digit milliseconds. That's why
deletes feel instantaneous and votes/reads are now too.

Things in the **dispatched path** need the full Python environment —
`/commands` build multi-line Markdown messages, `/digest` queries the
recommender for the weekly summary, `/reset` mutates `config.json`.
Workers are ephemeral, can't checkout the repo, can't run scikit-learn,
so we hand off to GitHub Actions. That takes ~30s for runner spin-up +
checkout + exec. The user-facing toast is already shown by then; the
state catches up in the background.

Pre-migration there was a third path: votes and reads went through *both*
the Worker (for the toast) and GitHub Actions (to mutate JSON). That
dispatch is gone — D1 supports an atomic `INSERT … ON CONFLICT DO UPDATE`
directly from the Worker, so there's no reason to round-trip through
GitHub for a single-row write.

### Concrete examples

**Tap *Delete*.** Telegram POSTs `h:delete:abc123`. Worker swaps keyboard
to Confirm/Cancel, sends toast "Confirm deletion?", returns 200 OK.
~300ms total, **no** GitHub run, **no** D1 write.

**Tap 👍 *Like*.** Telegram POSTs `v:like:abc123`. Worker sends toast
"Recorded 👍", then `ctx.waitUntil` upserts the `votes` row via the
`COALESCE` trick. No GitHub run.

**Send `/digest`.** Worker doesn't match any callback handler, falls
through to `dispatchToGitHub`. ~30s later `process_update.yml` finishes
and the digest message appears in Telegram.

### Infrastructure notes

- **Pay-per-request.** Cloudflare's free tier covers 100k req/day; this
  bot will never approach that.
- **V8 isolates, not containers.** Each request runs in an isolate that
  boots in milliseconds. Cold starts are effectively invisible.
- **Secrets are encrypted at rest.** `wrangler secret put TELEGRAM_TOKEN`
  stores it encrypted in Cloudflare's KV, decrypted only inside the
  running isolate.
- **Logs.** `npx wrangler@latest tail` from `worker/` streams live
  `console.log` output for every request.

## 6. Local development & debugging

```bash
# Read-only sanity check (no Telegram traffic, just hits D1)
LITFEED_DISABLE_POLL=1 \
CF_ACCOUNT_ID=… CF_D1_DATABASE_ID=… CF_D1_API_TOKEN=… \
python -c 'import state_store; print(state_store.load_config())'

# Run the daily pipeline locally (will actually send Telegram messages
# if TELEGRAM_TOKEN + CHAT_ID are set — be careful)
python main.py --commands-only   # process pending updates, skip fetch

# Inspect D1 directly
cd worker
npx wrangler d1 execute litfeed_state --remote \
  --command "SELECT bucket, COUNT(*) FROM votes GROUP BY bucket"

# Live Worker logs
npx wrangler tail
```

When something looks wrong:

1. `wrangler tail` to confirm the Worker is being hit.
2. `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo` to confirm
   Telegram is delivering (look at `last_error_message`).
3. GitHub repo → **Actions → "Process Telegram Update"** for any dispatch
   that needed Python.
4. `wrangler d1 execute litfeed_state --remote --command "…"` to inspect
   the row D1 thinks exists.

## 7. Why this design over alternatives

- **vs. running a server.** Worker is one file and one `wrangler deploy`.
  No 24/7 process, no autoscaling, no TLS, no monitoring.
- **vs. polling more frequently.** GitHub Actions has a 1-minute cron
  minimum; even at 1 min worst-case latency is 60s and you waste CI
  minutes 24/7.
- **vs. keeping all state in JSON in git** (the pre-migration design). Two
  workflows pushing within seconds of each other raced on `votes.json` /
  `reading_log.json` / `config.sent_ids` because git's textual rebase
  can't merge concurrent JSON appends. CI flapped, and callback votes had
  to wait ~30s for a GitHub Actions run before they were "saved".

D1 fixes both: every write is an atomic per-row upsert, and the Worker
can talk to D1 directly at the edge without involving GitHub.
