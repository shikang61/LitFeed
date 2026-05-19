# D1 migration runbook (historical)

> **Status: complete.** All phases below were executed. The JSON state
> files (`votes.json`, `reading_log.json`) are gone, `config.json` carries
> only `categories`, and `state_store.py` talks to D1 exclusively. This
> document is kept as a record of what was done in case the migration ever
> needs to be repeated for a fork or re-bootstrap.

LitFeed's runtime state used to live in Git-committed JSON files and now
lives in Cloudflare D1 (SQLite at the edge). The migration was sequenced
to keep every step reversible until the very end.

## Why

`config.json`, `votes.json` and `reading_log.json` are written by four
GitHub Actions workflows (`daily_papers`, `process_update`, `weekly_digest`,
`poll_commands`) and committed back to `main`. The textual `git rebase` in
each workflow can't merge concurrent appends to `sent_ids` / `liked` /
`disliked`, so two jobs that finish within seconds of each other produced
exit-code-128 rebase failures in CI. D1 lets each write be a per-row
`INSERT … ON CONFLICT DO UPDATE`, which is atomic and never races.

## Architecture after migration

```
User taps vote/Read button
        │
        ▼
Telegram  ──▶  Cloudflare Worker  ──▶  D1 (votes/reading_log)
                                  │
                                  └─▶  (only for /commands) repository_dispatch
                                       │
                                       ▼
                                  process_update.yml ──▶ D1 via REST
daily_papers.yml ───────────────────────────────────▶ D1 via REST
weekly_digest.yml ──────────────────────────────────▶ D1 via REST
```

`config.json` (categories only) stays in git, written rarely (`/reset`) and
pushed by `scripts/safe_state_push.sh` which is now a narrow band-aid for a
single file.

## One-time setup

### 1. Create the D1 database

```bash
cd worker
wrangler d1 create litfeed_state
```

Wrangler prints a `database_id`. Paste it into `worker/wrangler.toml`:

```toml
[[d1_databases]]
binding = "DB"
database_name = "litfeed_state"
database_id = "<paste-here>"
```

### 2. Apply the schema

```bash
wrangler d1 execute litfeed_state --remote --file ../migrations/0001_init.sql
```

Sanity check the tables landed:

```bash
wrangler d1 execute litfeed_state --remote --command "SELECT name FROM sqlite_master WHERE type='table'"
```

Expect: `votes`, `reading_log`, `sent_ids`, `last_batch`, `kv`.

### 3. Create a D1 REST API token

The Worker uses the native `env.DB` binding, but `main.py` runs on a GitHub
Actions runner and needs the HTTP API.

1. Cloudflare dash → My Profile → API Tokens → **Create Token**.
2. Use the **Custom token** template.
3. Permissions: **Account → D1 → Edit** (scoped to the LitFeed account).
4. Save the token value. You can't see it again.

### 4. Add GitHub repo secrets

In the repo settings → Secrets and variables → Actions, add:

| Secret | Value |
|---|---|
| `CF_ACCOUNT_ID`    | Cloudflare account id (dash sidebar) |
| `CF_D1_DATABASE_ID` | the id printed by `wrangler d1 create` |
| `CF_D1_API_TOKEN`  | the token from step 3 |

All three workflows (`daily_papers`, `process_update`, `weekly_digest`)
already reference them.

### 5. Backfill D1 from JSON

Run the migration once locally:

```bash
export CF_ACCOUNT_ID=...
export CF_D1_DATABASE_ID=...
export CF_D1_API_TOKEN=...
python scripts/migrate_to_d1.py --verbose
```

Verify row counts:

```bash
wrangler d1 execute litfeed_state --remote --command \
  "SELECT 'votes' AS table_name, COUNT(*) AS rows FROM votes
   UNION ALL SELECT 'reading_log', COUNT(*) FROM reading_log
   UNION ALL SELECT 'sent_ids', COUNT(*) FROM sent_ids
   UNION ALL SELECT 'last_batch', COUNT(*) FROM last_batch
   UNION ALL SELECT 'kv', COUNT(*) FROM kv"
```

They should match the JSON files (votes.liked + votes.disliked = D1 votes,
reading_log.papers = D1 reading_log, etc).

The migrate script is idempotent — re-run any time to top up D1 from the
canonical JSON.

## Rollout (sequenced)

The state_store layer supports a parallel-write phase so each cutover step
is reversible.

### Phase A — ship the code (no behaviour change)

1. Merge this PR. `main.py` now imports `state_store`. With the CF secrets
   not yet set in CI, `state_store` falls back to the JSON backend, so
   workflows behave exactly as before.
2. Watch CI for a day to confirm nothing regressed.

### Phase B — turn on parallel-write to D1

1. Add the `CF_*` secrets (steps 3–4 above) to the repo.
2. Trigger `daily_papers.yml` manually. Logs should show no JSON drift and
   D1 row counts should grow as votes/reads happen.
3. Leave reads on JSON for now (default).

### Phase C — flip reads to D1

Set a repository variable `LITFEED_READ_FROM=d1` in the workflows (or just
rely on the default once D1 is configured — `state_store._read_source`
picks `d1` when D1 is configured). Re-run the daily flow; verify `/stats`
returns the same numbers as before.

### Phase D — deploy Worker D1 writes

```bash
cd worker
wrangler deploy
```

After deploy, vote/Read button taps no longer fire `repository_dispatch`.
Watch the Workers dashboard logs for `[d1] recordVote failed` errors.
Confirm `process_update.yml` invocations drop sharply (commands only).

### Phase E — soak (~1 week)

JSON files keep updating in parallel. If anything goes wrong:

```bash
# emergency revert: read from JSON, keep dual-writes
LITFEED_READ_FROM=json
# or even fall back fully:
LITFEED_USE_LOCAL_JSON=1
```

Set either as a repo variable to override workflow behaviour without
redeploying.

### Phase F — cut JSON loose

After cutover verification:

1. Deleted `votes.json` and `reading_log.json` from the repo.
2. Trimmed `config.json` to just `{"categories": [...]}`.
3. Stripped `state_store.py` down to the D1-only path; removed
   `LITFEED_USE_LOCAL_JSON`, `LITFEED_DISABLE_JSON_WRITE`, and
   `LITFEED_READ_FROM` (no more dual-write modes).
4. Removed `scripts/merge_state.py` (only ever needed for multi-file
   JSON races, which can no longer happen).
5. Simplified `scripts/safe_state_push.sh` to a single-file commit-and-push
   loop for `config.json`. Dropped the commit step entirely from
   `daily_papers.yml` and `weekly_digest.yml`. `process_update.yml` still
   uses `safe_state_push.sh` for the rare `/reset` config write.

## Local development

D1 is mandatory. Export the same three `CF_*` env vars CI uses
(`CF_ACCOUNT_ID`, `CF_D1_DATABASE_ID`, `CF_D1_API_TOKEN`) and
`python main.py` will read/write the same database as production.
There is no JSON-file fallback.

## Open questions / risks

- **D1 REST latency from GitHub runners**: 50-150 ms US-East ↔ Cloudflare.
  A daily run does ~20-50 queries → 2-5 s added. Acceptable; ``_d1.batch``
  is used where it materially matters (last_batch, sent_ids cap).
- **D1 free tier**: 5M reads/day, 100k writes/day, 5 GB. LitFeed lives 3+
  orders of magnitude below all of these.
- **Worker D1 latency**: single-digit ms (same region). Vote toasts stay
  sub-second.
- **Reversibility (during migration only)**: the parallel-write phase kept
  the JSON files current so a `LITFEED_READ_FROM=json` flip would revert
  to pre-migration behaviour with no data loss. Post-Phase F that escape
  hatch no longer exists; the rollback path now is "restore D1 from a
  Cloudflare backup or re-run `scripts/migrate_to_d1.py` against an old
  JSON snapshot from git history".
